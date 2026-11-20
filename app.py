# -*- coding: utf-8 -*-
"""
讲座抢报助手 · 单窗口版
=====================================================================
一个窗口干完全部：几个人分别扫码 → 等开放 → 同时开火。

为什么只有一个窗口
---------------------------------------------------------------------
每个扫码的人需要一张只属于他的二维码。旧版是「一人一个黑窗口 + 一张
图片窗口」，四个人就是八个窗口叠在一起，根本分不清谁该扫哪张。
现在改成同一个窗口里轮换：扫完第一个人自动换成第二个人的码，不会串。

为什么多人能同时开火
---------------------------------------------------------------------
服务器按 access_token 限流（同一 token 约 2.8 秒一次），token 由「谁
扫的码」决定。三个人的微信 = 三个 token = 三条独立配额，可以同一刻发。
同一个人开两个窗口反而会互相挤配额。

凭证
---------------------------------------------------------------------
扫码换来的 token 只存在这个进程的内存里，退出即失效。
不写 token 文件，也不读别人留下的凭证。
=====================================================================
"""
import base64
import io
import json
import os
import queue
import random
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from email.utils import parsedate_to_datetime

try:
    import requests
    import rsa
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
except Exception as _e:                                    # pragma: no cover
    _err = '缺少运行库：%s' % _e
    try:
        import tkinter as tk
        from tkinter import messagebox
        _r = tk.Tk(); _r.withdraw()
        messagebox.showerror('讲座抢报助手', _err + '\n\n请联系发放程序的人。')
        _r.destroy()
    except Exception:
        pass
    sys.exit(1)

APP_NAME = '讲座抢报助手'
APP_VER = '更新于 11-19 18:28'
API = 'https://api-xcx-qunsou.weiyoubot.cn'

# 客户版只允许一个人。开关在 single_mode.py 里，打包时已写死进 exe，
# 客户改不了；本机自用的多人版是 False，行为完全不变。
try:
    from single_mode import SINGLE_MODE
except Exception:
    SINGLE_MODE = False

RSA_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCjI8E8LT0fwFekelMMuTWuaIfo
fK69lyNIo+Vz0CGdfE3rLSIH94S2A3Q+bg+9/VnImvfXzcDVmqwHwC4hHPHs6hc6
ufq0gfivTPms3kwX74F5qLMr70j4iZLt/PCkU+uyQ56KmRW4foCV4RPX8o8QZVss
6eifHaeUeJxKM556ewIDAQAB
-----END PUBLIC KEY-----"""
_PUB = rsa.PublicKey.load_pkcs1_openssl_pem(RSA_PUB_PEM.encode())

LOGIN_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'zh-CN,zh;q=0.9',
    'origin': 'https://p.baominggongju.com',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0',
}
SUBMIT_HEADERS = {
    'accept': '*/*',
    'accept-language': 'zh-CN,zh;q=0.9',
    'content-type': 'application/json',
    'referer': 'https://servicewechat.com/wxfaa08012777a431e/1173/page-frame.html',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781'
                  '(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF '
                  'WindowsWechat(0x63090a13) XWEB/8555',
    'xweb_xhr': '1',
}

OK_MSGS = {'', '提交次数超过限制', '活动期间，只允许提交1次', '您已经报过名了', '已经报过名'}
DEAD_MSGS = {'活动已截止', '报名已截止', '活动已结束', '报名人数已满或项目数量不足',
             '活动名额已满', 'invalid parameter'}
HARD_STOP_KEYS = ('登录', 'token', 'Token', 'TOKEN')

FW = {ord(a): b for a, b in zip('０１２３４５６７８９', '0123456789')}
FW.update({ord('＋'): '+', ord('－'): '-', ord('×'): '*', ord('÷'): '/',
           ord('＝'): '=', ord('　'): ' ', ord('（'): '(', ord('）'): ')', ord('：'): ':'})


# ============================================================ 路径
def app_dir():
    """打包成 exe 后取 exe 所在目录，否则取脚本目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = app_dir()
PEOPLE_PATH = os.path.join(HERE, 'bmgj_people.json')
LOG_PATH = os.path.join(HERE, '运行日志.log')


# ============================================================ 协议工具
def solve_math(text):
    """字段名里藏的加减法验证码，本地算，不把算式交上去"""
    if not text:
        return None
    t = str(text).translate(FW)
    exprs = re.findall(r'\d+\s*(?:[+\-*/])\s*\d+(?:\s*(?:[+\-*/])\s*\d+)*', t)
    if not exprs:
        return None
    expr = max(exprs, key=len).replace(' ', '')
    try:
        v = eval(expr, {'__builtins__': {}}, {})
    except Exception:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def extract_eid(text):
    """
    从二维码解出的内容里抠活动号。

    报名链接形如 http://m.baominggongju.com/?eid=6aeac5d05a8fcf02f37c5997&_referer=...
    也可能直接就是一小段纯 eid。所以分两种：
    1) 有 eid= 参数 → 取它
    2) 整段是一串 24 位十六进制（eid 的固定长相）→ 直接用
    """
    if not text:
        return None
    t = str(text).strip()
    m = re.search(r'[?&]eid=([0-9a-fA-F]{20,32})', t)
    if m:
        return m.group(1)
    m = re.search(r'\b([0-9a-fA-F]{24})\b', t)
    if m:
        return m.group(1)
    return None


def clean_eid(text):
    """
    把用户从各处粘来的东西规范成纯 eid。

    实测常见的脏写法（2026-11-06 用户粘进来的）：
      ?eid=6aedc975ecf33e38e704f71b     ← 从链接里只复制了后半截
      6aedc975ecf33e38e704f71b&_referer=x ← 尾巴带参数
      6aedc975ecf33e38e704f71b           ← 干净的

    认不出来就返回空串，交给程序「扫完第一个人自动找活动」。
    """
    t = str(text or '').strip()
    if not t:
        return ''
    e = extract_eid(t)
    if e:
        return e
    t = re.sub(r'^.*?[?&]eid=', '', t, flags=re.I)
    t = re.split(r'[&#?\s]', t)[0].strip()
    if re.fullmatch(r'[0-9a-fA-F]{16,32}', t):
        return t
    return ''


def decode_qr_text(pil_img):
    """
    解一张 PIL 图片里的普通二维码，返回文本或 None。

    用 pyzbar（轻量，自带 zbar DLL，只吃 PIL.Image、不依赖 numpy/cv2，
    这样打包体积才不会暴涨）。解不出返回 None。

    只解标准二维码；真正的微信小程序码（太阳码）解不了。
    """
    try:
        from pyzbar import pyzbar
        res = pyzbar.decode(pil_img)
        if res:
            return res[0].data.decode('utf-8', 'replace')
    except Exception:
        pass
    return None


_sig = {}


def h5_sign(eid):
    """网页版 _a 签名：RSA(eid + 当前秒)。同一秒内复用"""
    sec = int(time.time())
    if sec in _sig:
        return _sig[sec]
    b = base64.b64encode(rsa.encrypt((eid + str(sec)).encode(), _PUB)).decode()
    _sig.clear()
    _sig[sec] = b
    return b


def measure_offset(s, budget=4.0):
    """观察 Date 头整秒跳变求 offset = 服务器时间 - 本机时间

    返回 (offset, prec)，prec 是这半个周期的时间跨度估计，越小越可信。
    只有当「确实观察到了整秒跳变」时才算测得准；抓不到跳变说明这段时间
    网络抖动或样本太少，prec 会被标得很大，调用方要据此放弃这次的数。
    """
    time.sleep(random.uniform(0.0, 0.35))
    samples, prev = [], None
    t_end = time.time() + budget
    while time.time() < t_end and len(samples) < 400:
        t_send = time.time()
        try:
            d = s.head(API, timeout=3).headers.get('Date')
        except Exception:
            time.sleep(0.05)
            continue
        t_recv = time.time()
        if not d:
            continue
        ts = parsedate_to_datetime(d).timestamp()
        samples.append((ts, t_send, t_recv))
        if prev is not None and ts == prev[0] + 1:
            lo, hi = prev[2], t_send
            return ts - (lo + hi) / 2.0, (hi - lo) / 2.0 + 0.02
        prev = (ts, t_send, t_recv)
        time.sleep(0.06)
    # 没抓到整秒跳变 = 这次测不准。不要硬给一个近似值去冒充准的，
    # prec 直接标成 99.0，调用方看到超过阈值就会放弃它。
    return max([ts - tr for ts, _, tr in samples], default=0.0), 99.0


def is_captcha_field(fname):
    """
    只有明显是算式验证码的字段才求解。

    不加这道闸会出大事：像「1-2节」「第3-4周」这种字段名里也有
    “数字-数字”，直接扔进 solve_math 会被算成 -1，然后原样提交上去，
    字段值就错了，整条报名可能被判无效而失败。

    2026-11-19 补：整段就是算式的字段名（如「8+9」，没有等号）也算验证码。
    用 fullmatch 而不是 search，「1-2节」这种不会误伤。
    """
    if ('=' in fname) or ('计算' in fname) or ('验证' in fname):
        return True
    return bool(re.fullmatch(r'\s*\d+\s*[+\-*/]\s*\d+(?:\s*[+\-*/]\s*\d+)*\s*',
                             fname or ''))


def person_delay(p):
    """这个人相对开放时刻推迟多少秒提交。没设就是 0（准时）。"""
    try:
        v = float(p.get('delay') or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(-30.0, min(120.0, v))


def build_info(reqs, person):
    """字段映射：字段名的每个字都出现在服务器字段名里就算命中（学院 → 学院全称）"""
    mapping = {'姓名': person.get('name', ''),
               '学号': person.get('sid', ''),
               '学院': person.get('college', ''),
               '手机号': person.get('phone', ''),
               '电话': person.get('phone', ''),
               '班级': person.get('klass', ''),
               '年级': person.get('grade', '')}
    info, unmapped = [], []
    for it in reqs:
        fname = str(it.get('field_name', ''))
        fset = set(fname)
        val = None
        for k, v in mapping.items():
            if v and set(k).issubset(fset):
                val = v
                break
        if val is None and is_captcha_field(fname):
            val = solve_math(fname)
        opts = it.get('options') or []
        choose = (it.get('type_text') or '') in ('单项选择', '多项选择')
        if val is not None and opts and choose:
            texts = [o.get('text', o) if isinstance(o, dict) else str(o) for o in opts]
            hit = next((t for t in texts if val == t or val in t or t in val), None)
            if hit:
                val = hit
        if val is None and opts and choose:
            # 认不出来的选择题，以前统一填 '1'。可选项里根本没有 '1' 这个值，
            # 服务器会直接回 invalid parameter，整条报名作废。
            # 挑第一个选项至少能过校验（明天那场万一多出没见过的题目，靠它兜底）。
            o0 = opts[0]
            if isinstance(o0, dict):
                val = o0.get('text') or o0.get('value') or o0.get('label') or None
            else:
                val = str(o0) or None
        if val is None:
            if is_captcha_field(fname):
                # 算式验证码算不出来也无所谓：服务器拿它防机器人，不校验答案。
                # 只有这一类允许填 1 兜底（2026-11-19 用户明确）。
                val = '1'
            else:
                # 其他字段一律不能默认 1。手机号、学院、班级填成 "1" 是假信息，
                # 报名会带着错资料交出去，自己还以为成功了。
                # 宁可留空让服务器当场拒绝，也不要静默造假。
                unmapped.append(fname)
                val = ''
        info.append({'field_name': fname,
                     'field_value': val if val is not None else '',
                     'field_key': it.get('field_key', ''), 'ignore': 0})
    return info, unmapped


def fetch_fields(s, tok, eid):
    try:
        j = s.get('%s/xcx/enroll/v1/req_detail' % API,
                  params={'access_token': tok, 'eid': eid}, headers=LOGIN_HEADERS, timeout=5).json()
        reqs = (j.get('data') or {}).get('req_info') or []
        if reqs:
            return reqs
        if any(k in str(j.get('msg') or '') for k in HARD_STOP_KEYS):
            return None
    except Exception:
        pass
    try:
        d = (s.get('%s/xcx/enroll/v3/detail' % API,
                   params={'access_token': tok, 'eid': eid},
                   headers=LOGIN_HEADERS, timeout=5).json().get('data') or {})
        for k in ('req_info', 'req_list', 'fields'):
            v = d.get(k)
            if isinstance(v, list) and v:
                return v
    except Exception:
        pass
    return []


def userinfo(s, tok):
    try:
        d = s.get('%s/xcx/enroll/v1/userinfo' % API,
                  params={'access_token': tok}, headers=LOGIN_HEADERS, timeout=8).json()
        if d.get('msg') == 'ok':
            return d.get('data') or {}
    except Exception:
        pass
    return None


def history(s, tok):
    try:
        v = s.get('%s/xcx/enroll/v1/user/history' % API,
                  params={'access_token': tok}, headers=LOGIN_HEADERS, timeout=8).json().get('data')
        return v if isinstance(v, list) else []
    except Exception:
        return []


# 微信默认昵称。撞名太正常了，靠它判「是不是同一个微信」会冤枉人
# （2026-11-06 22:00：有两位同学都叫「微信用户」，工具直接判「必失败」，
# 但两人可能是各拿各的手机扫的）。
GENERIC_NICKS = {'微信用户', '微信', 'wechat', '微信用户1', '用户', 'unknown'}


def who_of(ui):
    """
    把 userinfo 的返回压成 (显示名, 判重用的唯一标识)。

    能用 uid 就绝不用昵称：同一个微信扫两次，uid 必相同；
    但昵称可以重名，尤其所有人都还是默认的「微信用户」。
    返回的标识带前缀，'uid:' = 确定同号，'nick:' = 只能算疑似。
    """
    ui = ui or {}
    nick = (ui.get('name') or ui.get('nickname') or '').strip()
    for k in ('uid', 'user_id', 'userId', 'openid', 'unionid', 'open_id', 'id'):
        v = ui.get(k)
        if v not in (None, '', 0, '0'):
            return nick, 'uid:%s' % v
    if nick:
        return nick, 'nick:%s' % nick
    return '', ''


def short_detail(s, eid, tries=3):
    """
    活动详情。一定要重试：
    这个接口只要抖一下，start_time 就变 0，程序会以为「活动已开放」
    然后立刻开火 —— 那是最坏的情况，一发打在开放前，之后全是空。
    """
    for k in range(tries):
        try:
            d = s.get('%s/xcx/enroll/v1/short_detail' % API,
                      params={'eid': eid, 'referer': '', 'spider': 'h5'},
                      timeout=8).json().get('data') or {}
            if d.get('start_time'):
                return d
            if k == tries - 1:
                return d
        except Exception:
            pass
        time.sleep(0.4 * (k + 1))
    return {}


# ============================================================ 二维码
def _font(size):
    for p in (r'C:\Windows\Fonts\msyhbd.ttc', r'C:\Windows\Fonts\msyh.ttc',
              r'C:\Windows\Fonts\simhei.ttf'):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def qr_image(raw_bytes, name, sid, order, total):
    """把二维码画成一张带姓名的卡片，扫错就不可能"""
    im = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
    im = im.resize((im.width * 2, im.height * 2), Image.NEAREST)
    W = im.width
    top, bottom = 86, 92
    canvas = Image.new('RGB', (W, im.height + top + bottom), 'white')
    canvas.paste(im, (0, top))
    d = ImageDraw.Draw(canvas)
    f1, f2, f3 = _font(44), _font(28), _font(24)

    def center(txt, y, font, fill):
        w = d.textlength(txt, font=font)
        d.text(((W - w) / 2, y), txt, font=font, fill=fill)

    center('第 %d/%d 位  %s' % (order, total, name), 16, f1, (190, 20, 20))
    center('学号 %s' % sid, canvas.height - bottom + 14, f2, (60, 60, 60))
    center('请用本人的微信扫这个码', canvas.height - bottom + 52, f3, (150, 40, 40))
    return canvas


# ============================================================ 抢报引擎
class Engine:
    """
    在后台线程里跑完整流程，所有界面动作通过 emit 丢回主线程。
    kind: log / qr / state / info / countdown / done / alert
    """

    def __init__(self, people, eid, keyword, dry, emit, stop, skip_flags=None,
                 delay=0.0, gap=3.0, window=45.0, cache=None, max_people=99,
                 strict_lic=False):
        self.people = people
        self.eid = eid
        self.keyword = keyword
        self.dry = dry
        self.emit = emit
        self.stop = stop
        self.max_people = int(max_people or 99)   # 授权允许的人数上限
        # 只有「客户授权（带 n 字段）」才在开火前再验一次；
        # 本机白名单不受影响，行为和以前完全一致。
        self.strict_lic = bool(strict_lic)
        self.skip_flags = skip_flags if skip_flags is not None else {}
        self.delay = float(delay)        # T0 之后再等几秒发（正=晚，负=提前）
        self.gap = float(gap)            # 被限流后补射的间隔
        self.window = float(window)      # 开火后最多纠缠多少秒
        self.sessions = {}
        # 扫过的二维码跨「开始」保留：同一份名单不该每次都重扫一遍。
        # cache 由界面层持有，key = (姓名, 学号)——改了姓名/学号视作换了个人，
        # 必须重扫；只改学院不影响，token 照样能用。
        self.cache = cache if cache is not None else {}
        self.tokens = {}
        self.nicks = {}
        # 判重用：i -> 'uid:xxx' / 'nick:昵称'。跟 nicks 分开存，
        # 因为显示归显示，判重必须尽量用唯一值。
        self.whos = {}
        self.infos = {}
        self.start_ts = 0.0
        self.title = ''
        self.limit = None
        self.off = 0.0

    # ---------------- 小工具
    def log(self, msg):
        self.emit('log', msg)

    def sess(self, i):
        if i not in self.sessions:
            s = requests.Session()
            s.headers.update({'Connection': 'keep-alive'})
            self.sessions[i] = s
        return self.sessions[i]

    def state(self, i, text, color=''):
        self.emit('state', (i, text, color))

    def dup_of(self, i):
        """
        第 i 个人跟别人撞号了吗？返回 (另一个人的下标, 是否确定, 那个人的名字)。

        确定 = 比的是 uid，同一微信扫两次 uid 一定一样，那就是真撞了；
        不确定 = 只能比昵称，而「微信用户」这种默认名谁都可以叫，
               撞名不等于同一个号，需要人自己判断。
        """
        who = self.whos.get(i)
        if not who:
            return None, False, ''
        for j in sorted(self.whos):
            if j == i:
                continue
            if self.whos[j] == who:
                nm = self.people[j]['name'] if j < len(self.people) else '?'
                return j, who.startswith('uid:'), nm
        return None, False, ''

    def say_dup(self, i, name):
        """扫码成功后统一喊一嗓子：撞了就按确定/疑似两种口气说清楚。"""
        j, sure, other = self.dup_of(i)
        if j is None:
            return False
        nick = self.nicks.get(i) or ''
        if sure:
            self.log('!! %s 和 %s 是同一个微信（%s）' % (name, other, nick))
            self.log('!! 一个微信在一个活动只能报一次，后报的那个一定被拒。'
                     '请给其中一个换台手机重扫。')
            self.state(i, '同一微信 · 必失败', '#c62828')
        else:
            self.log('? %s 和 %s 的微信昵称都是「%s」，是默认名，不一定是同一个号'
                     % (name, other, nick))
            self.log('? 如果这两人各用自己的手机扫的，忽略这条；'
                     '如果是一台手机连扫两下，后报的会被拒，换一台重扫。')
            self.state(i, '昵称相同 · 请核对', '#ef6c00')
        return True

    # ---------------- 主流程
    def run(self):
        try:
            self._run()
        except Exception as e:
            self.log('!! 出错：%s' % e)
            self.log(traceback.format_exc().strip().splitlines()[-1])
            self.emit('alert', '出错了：%s' % e)
            self.emit('done', None)

    def _run(self):
        n = len(self.people)
        self.log('=' * 54)
        self.log('%s · 共 %d 人' % (APP_NAME, n))
        self.log('=' * 54)

        # ---- 1. 一次给所有人出码：谁扫哪张都行，不排队
        self._scan_all()

        if self.stop.is_set():
            self.emit('done', None)
            return
        if not self.tokens:
            self.log('没有任何人扫码成功，停止')
            self.emit('done', None)
            return

        # 没填活动号就借任意一个已扫账号去找
        if not self.eid:
            for i in sorted(self.tokens):
                self.eid = self._resolve_eid(i, self.people[i])
                if self.eid:
                    break
            if not self.eid:
                self.log('!! 找不到活动。请在「活动号」里直接填 eid 后重新开始')
                self.emit('alert', '找不到活动，请填活动号（eid）后重试')
                self.emit('done', None)
                return
        self.log('扫码完成：%d/%d 人就绪（已就绪的人下次点开始不用再扫）'
                 % (len(self.tokens), n))

        # ---- 2. 活动信息（用任意一个可用会话，第一个人可能被跳过）
        s0 = self.sess(sorted(self.tokens.keys())[0])
        d = short_detail(s0, self.eid)
        self.start_ts = float(d.get('start_time') or 0)
        self.title = d.get('title') or ''
        self.limit = d.get('limit')
        self.emit('info', {'eid': self.eid, 'title': self.title,
                           'start': self.start_ts, 'limit': self.limit})
        self.log('活动：%s' % (self.title or '(无标题)'))
        if self.start_ts:
            self.log('开放：%s   名额：%s'
                     % (datetime.fromtimestamp(self.start_ts).strftime('%m-%d %H:%M:%S'),
                        self.limit))
        else:
            self.log('!! 三次都没取到开放时间，按「已经在报名中」处理')
            self.log('   如果其实还没开放，会先收到「未开始」，程序会逐步退避重试，')
            self.log('   不会猛刷把限流额度烧光。但最好还是把活动号填对。')

        # ---- 3. 每人取字段
        for i, p in enumerate(self.people):
            tok = self.tokens.get(i)
            if not tok:
                continue
            reqs = fetch_fields(self.sess(i), tok, self.eid)
            if reqs is None:
                self.log('!! %s 的登录态被拒，请重来' % p['name'])
                self.state(i, '登录失效', '#c62828')
                continue
            info, unmapped = build_info(reqs, p)
            if not info:
                # 字段空着提交上去必然失败，而且还会白占一次限流配额。
                # 记下来，开放前 5 秒开始补取（活动开放后字段才会出来）。
                self.log('%s：现在还没拿到表单（活动没开放，服务器不给字段），'
                         '这是正常的，不是失败；开放前会自动补取' % p['name'])
                self.state(i, '待补字段·正常', '#1565c0')
                continue
            self.infos[i] = info
            self.log('%s：表单 %d 项%s' % (
                p['name'], len(info),
                ('，未匹配已留空：' + '、'.join(unmapped)) if unmapped else ''))
            for it in info:
                self.log('    %-12s -> %s' % (it['field_name'], it['field_value']))

        if not self.infos:
            # 活动没开放时服务器根本不给字段，这不是失败，是常态。
            # 以前这里直接 return，等于「09:00 之前点开始 = 立刻停止」，
            # 抢报永远开始不了（2026-11-06 22:04 实测撞到）。
            # 只要有人扫过码，就继续往下走：后面会在开放前 5 秒自动补取。
            if not self.tokens:
                self.log('!! 一个人都没就绪（没扫码），停止')
                self.emit('done', None)
                return
            self.log('字段还没出来（活动没开放时服务器不给），先等着；')
            self.log('开放前 5 秒会自动补取，不影响开火时刻，不用管这条')

        # ---- 4. 对时
        self.off, prec = measure_offset(s0)
        if abs(self.off) > 60:
            self.log('!! 对时异常(%+.0fs)，改用本机时间' % self.off)
            self.off = 0.0
        self.log('对时：服务器 - 本机 = %+.3f 秒（±%.3f）' % (self.off, prec))

        # ---- 5. 等到开火时刻
        if self.start_ts and self.start_ts > time.time() + 1:
            fire_at = self.start_ts - self.off - 0.06 + self.delay
            self.log('等待开放，还剩 %.0f 秒' % (self.start_ts - time.time()))
            if abs(self.delay) >= 0.05:
                self.log('（已设发射偏移 %+.2f 秒）' % self.delay)
            while time.time() < fire_at - 4.0:
                if self.stop.is_set():
                    self.log('已手动停止')
                    self.emit('done', None)
                    return
                left = fire_at - 4.0 - time.time()
                self.emit('countdown', left + 4.0)
                time.sleep(min(0.3, max(0.02, left)))
            # 2026-11-19 15:00 那场：临开火前 4 秒这里曾再测一次对时并覆盖，
            # 结果 2.5 秒短采样把 -2.067 错算成 -3.241，把发射拖晚了 1 秒多。
            # 本机与服务器都是 NTP 同步的，偏移不会在等待期间自己漂；临阵短
            # 采样只会测得更差。所以彻底删掉临阵重测，发射时刻锁死第一次对时值。
        else:
            fire_at = time.time() + 1.0 + max(0.0, self.delay)
            self.log('活动已在报名中，%.1f 秒后提交' % (1.0 + max(0.0, self.delay)))

        # 临开火刷新字段（算式验证码会变）
        for i in list(self.infos.keys()):
            reqs = fetch_fields(self.sess(i), self.tokens.get(i), self.eid)
            if reqs:
                info, _ = build_info(reqs, self.people[i])
                if info:
                    self.infos[i] = info

        # 之前字段是空的那几个人：开放前 5 秒开始补取。
        # 不能等到开火那一刻才取 —— 那样取一次要 0.3~1 秒，
        # 等于把这一枪整体往后推，名额紧的时候就是白丢。
        missing = [i for i in self.tokens if i not in self.infos]
        if missing:
            t_try = fire_at - 5.0
            if t_try > time.time():
                self.log('%d 人还没拿到表单，开放前 5 秒开始自动补取（不影响开火时刻）'
                         % len(missing))
                while time.time() < t_try:
                    if self.stop.is_set():
                        break
                    self.emit('countdown', fire_at - time.time())
                    time.sleep(0.004)
            else:
                self.log('%d 人还没拿到表单，马上开始补取' % len(missing))
            # 最多补到开火后 2 秒就收手。以前给到 8 秒，
            # 万一字段一直不出来，会把所有人的开火整体拖晚 8 秒。
            t_end = fire_at + 2.0
            while missing and time.time() < t_end and not self.stop.is_set():
                for i in list(missing):
                    reqs = fetch_fields(self.sess(i), self.tokens.get(i), self.eid)
                    if reqs:
                        info, un = build_info(reqs, self.people[i])
                        if info:
                            self.infos[i] = info
                            missing.remove(i)
                            nick = self.nicks.get(i) or ''
                            self.state(i, '已就绪' + ((' · ' + nick) if nick else ''), '#2e7d32')
                            self.log('%s：补取到字段（%d 项），已就绪'
                                     % (self.people[i]['name'], len(info)))
                            if un:
                                self.log('    !! %d 个字段没对上（%s），已留空，不填 1'
                                         % (len(un), '、'.join(un[:6])))
                            # 把实际要提交的内容写出来。开放那一瞬间服务器
                            # 偶尔会给出残缺的字段（缺 field_key / 值不在选项里），
                            # 提交就回 invalid parameter。不落明细就永远查不出
                            # 到底是哪一项不对（2026-11-06 22:15 有位同学就是这种情况，
                            # 同一批补取的另一位却一次过）。
                            try:
                                self.log('    %s 将提交：%s'
                                         % (self.people[i]['name'],
                                            json.dumps(info, ensure_ascii=False)[:400]))
                            except Exception:
                                pass
                if missing:
                    time.sleep(0.35)
            for i in missing:
                self.state(i, '拿不到表单', '#c62828')
                self.log('!! %s 一直拿不到表单字段，跳过' % self.people[i]['name'])

        if not self.infos:
            self.log('!! 没有任何人有可用字段，停止')
            self.emit('done', None)
            return

        # 每人的真实发射时刻 = 开火时刻 + 本人秒数。
        # 线程提前 2 秒起来，各自等自己的点；这样运行中把某人的秒数
        # 往小改（提前）也还来得及，不用重新扫码。
        offs = {i: person_delay(self.people[i]) for i in self.infos}
        start_mark = min(fire_at - 2.0, fire_at + min([0.0] + list(offs.values())))
        off_note = [i for i in offs if abs(offs[i]) >= 0.05]
        if off_note:
            self.log('个人发射秒数：' + '   '.join(
                '%s %+.1fs' % (self.people[i]['name'], offs[i]) for i in off_note))
        # 开火前最后一遍查重：两个名额一个微信，后面那个一定白打。
        # 比的是 uid（没有才退回昵称），撞了就只提醒，不拦——用户可能明知
        # 而为之，拦下来反而挡了正常报名。
        _seen = {}
        for i in self.infos:
            nk = (self.whos or {}).get(i) or ''
            if not nk:
                continue
            if nk in _seen:
                a, b = _seen[nk][0], self.people[i]['name']
                sure = _seen[nk][1]
                if sure:
                    self.log('!! %s 和 %s 是同一个微信（%s），%s 这一发必然被拒'
                             % (a, b, self.nicks.get(i) or '', b))
                else:
                    self.log('? %s 和 %s 昵称相同（%s），若是一台手机扫的，%s 会被拒'
                             % (a, b, self.nicks.get(i) or '', b))
            else:
                _seen[nk] = (self.people[i]['name'], nk.startswith('uid:'))

        for i in self.infos:
            self.state(i, '待发射 %s' % datetime.fromtimestamp(
                fire_at + offs[i]).strftime('%H:%M:%S'), '#1565c0')

        while time.time() < start_mark:
            if self.stop.is_set():
                self.log('已手动停止')
                self.emit('done', None)
                return
            self.emit('countdown', max(0.0, fire_at - time.time()))
            time.sleep(0.002)

        # 开火前预热连接：2026-11-19 15:00 那场，有位同学第一发 POST 花了 1613ms
        # （平时 400ms），多出来的就是 TLS 握手 + TCP 建连的冷启动。前面的
        # fetch_fields 走的是 GET 接口，连接池里没有「提交接口已就绪的连接」。
        # 这里在真正开火前，用各账号自己的 Session 对提交接口同域发一个轻量
        # HEAD，把握手先做完；第一发 POST 就能复用热连接，把 1.6 秒压回几百毫秒。
        self._warmup_connections()

        # ---- 6. 演练：探测提交通道（老版 probe 的能力，之前漏了）
        if self.dry:
            self.probe_channel()
            self.emit('done', None)
            return

        # ---- 6.5 开火前二次校验授权（只对「带人数限制的客户授权」生效）
        # 自己这台机器走白名单、授权里没有 n 字段，strict_lic=False，
        # 整段直接跳过 —— 你这边和加这个功能之前一模一样，不会多一次校验、
        # 也不会因为机器码临时读不到而误停。
        if self.strict_lic:
            try:
                import auth_core
                _lic2 = auth_core.check(self.eid)
                if not _lic2.get('ok'):
                    self.log('!! 开火前授权校验未通过（%s），已停止'
                             % _lic2.get('reason'))
                    self.emit('done', None)
                    return
            except Exception as _e:
                self.log('!! 开火前授权校验异常：%s' % _e)

        if len(self.infos) > self.max_people:
            self.log('!! 这份授权只允许 %d 个人，本次有 %d 个，已停止'
                     % (self.max_people, len(self.infos)))
            self.emit('done', None)
            return

        # ---- 7. 各按自己的秒数开火（每人独立 token，互不占额度）
        if off_note:
            self.log('>>> 开火（%d 人，按各自秒数）' % len(self.infos))
        else:
            self.log('>>> 开火（%d 人同时）' % len(self.infos))
        self.emit('countdown', 0)
        results = {}
        ths = []
        for i, info in self.infos.items():
            t = threading.Thread(target=self._fire_one,
                                 args=(i, self.people[i], self.tokens.get(i), info,
                                       fire_at, results), daemon=True)
            t.start()
            ths.append(t)
        for t in ths:
            t.join(timeout=90)

        # ---- 7. 汇总
        self.log('=' * 54)
        okn = 0
        for i, p in enumerate(self.people):
            r = results.get(i)
            if r and r[0]:
                okn += 1
                self.log('  ✓ %s  报名成功' % p['name'])
            elif r:
                self.log('  ✗ %s  %s' % (p['name'], r[1]))
            else:
                self.log('  – %s  未提交' % p['name'])
        self.log('=' * 54)
        self.log('成功 %d / %d' % (okn, len(self.people)))
        self.emit('done', {'ok': okn, 'total': len(self.people)})

    # ---------------- 演练：探通道
    def probe_channel(self):
        """
        用一个假活动号真发一次 POST。

        为什么要真发：扫码成功只说明登录态拿到了，不代表**提交接口**接受
        这个 token 和签名。11-02 那次就是扫码一切正常、提交被拒。
        假 eid 不存在，服务端只会回「活动不存在」，不会产生任何报名。
        """
        self.log('')
        self.log('--- 演练：探测提交通道（假活动号，不会真的报名）---')
        ok = 0
        for i, p in enumerate(self.people):
            tok = self.tokens.get(i)
            if not tok:
                continue
            fake = 'probe' + hex(int(time.time() * 1000))[2:]
            payload = {'access_token': tok, 'eid': fake, 'info': [], 'on_behalf': 0,
                       'items': [], 'referer': '', 'from': 'h5', 'vip_role': 'no',
                       '_a': h5_sign(fake)}
            try:
                j = self.sess(i).post('%s/xcx/enroll/v5/enroll' % API, headers=SUBMIT_HEADERS,
                                      data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                      timeout=8).json()
                msg = str(j.get('msg') or '')
            except Exception as e:
                msg = 'EXC:%s' % e
            if any(k in msg for k in HARD_STOP_KEYS):
                self.log('  ✗ %s 通道被拒：%r' % (p['name'], msg))
                self.state(i, '通道被拒', '#c62828')
            else:
                ok += 1
                self.log('  ✓ %s 通道可用（服务端回 %r，属于"活动不存在"类）'
                         % (p['name'], msg))
                self.state(i, '通道可用', '#2e7d32')
            time.sleep(1.0)
        self.log('--- 演练结束：%d/%d 人通道可用 ---' % (ok, len(self.people)))
        self.log('说明：演练不会写入任何报名记录，真正的提交仍在正式运行时。')

    # ---------------- 单个人扫码
    def should_skip(self, i):
        return bool(self.skip_flags.get(i))

    def _token_alive(self, s, tok):
        """这个 token 还能用吗（用来决定要不要让人重新扫码）。"""
        try:
            ui = userinfo(s, tok) or {}
            return bool(ui.get('name') or ui.get('nickname') or ui.get('uid') or ui)
        except Exception:
            return False

    def _new_code(self, i, codes):
        """给第 i 个人出一张登录码，并立刻画到他自己的卡片里。"""
        p = self.people[i]
        s = self.sess(i)
        for _ in range(3):
            if self.stop.is_set():
                return False
            try:
                j = s.get('%s/xcx/enroll_web/v1/pc_code' % API,
                          headers=LOGIN_HEADERS, timeout=10).json()
            except Exception:
                time.sleep(0.8)
                continue
            if j.get('msg') != 'ok':
                time.sleep(0.8)
                continue
            data = j.get('data') or {}
            code = data.get('code')
            qr = data.get('qrcode') or ''
            if not code:
                time.sleep(0.5)
                continue
            raw = base64.b64decode(qr.split(',', 1)[-1] if ',' in qr else qr)
            # 直接用服务器原图：卡片上已经有姓名学号了，图上再印一遍反而
            # 会把二维码挤小，200px 下不好扫。
            try:
                img = Image.open(io.BytesIO(raw))
            except Exception:
                time.sleep(0.5)
                continue
            codes[i] = {'code': code, 't': time.time()}
            self.emit('qr', (i, img))
            self.state(i, '等待扫码', '#ef6c00')
            self.log('请 %s 扫码（扫哪张都行，不用按顺序）' % p['name'])
            return True
        self.state(i, '出码失败', '#c62828')
        self.log('!! %s 的二维码取不到，稍后自动重试' % p['name'])
        return False

    def _scan_all(self):
        """
        一次把所有人的码都出出来，谁先扫、扫哪张都行。
        出码串行 + 间隔 0.45 秒；轮询轮流查（三人一轮约 0.9 秒）。
        2026-11-06 实测：pc_code 连发 4 次全 ok，pc_login 三人轮流也不限流。
        """
        todo = []
        for i, p in enumerate(self.people):
            if self.should_skip(i):
                self.state(i, '已跳过（没勾选）', '#757575')
                continue
            hit = self.cache.get(p['name'])
            if hit and hit.get('tok') and self._token_alive(self.sess(i), hit['tok']):
                self.tokens[i] = hit['tok']
                self.nicks[i] = hit.get('nick') or ''
                who = hit.get('who') or ''
                if not who:
                    # 老缓存只存了昵称，补问一次 userinfo 拿 uid；
                    # 不然判重只能靠昵称，重名的人会被冤枉
                    who = who_of(userinfo(self.sess(i), hit['tok']))[1]
                    hit['who'] = who
                if who:
                    self.whos[i] = who
                self.state(i, '已就绪 · ' + (hit.get('nick') or ''), '#2e7d32')
                self.emit('scanned', (i, hit.get('nick') or ''))
                self.log('%s 上次扫过了（%s），这次不用再扫'
                         % (p['name'], hit.get('nick') or '已登录'))
                self.say_dup(i, p['name'])
                continue
            todo.append(i)

        if not todo:
            return

        codes = {}
        for i in list(todo):
            if self.stop.is_set():
                return
            self._new_code(i, codes)
            time.sleep(0.45)

        deadline = time.time() + 480
        last_tick = 0
        while todo and time.time() < deadline and not self.stop.is_set():
            for i in list(todo):
                if self.should_skip(i):
                    self.state(i, '已跳过', '#757575')
                    todo.remove(i)
                    continue
                if i not in codes:
                    # 刚才没出成功，过半分钟再试
                    if time.time() - last_tick > 30:
                        self._new_code(i, codes)
                    continue
                if time.time() - codes[i]['t'] > 360:      # 6 分钟换一张
                    self._new_code(i, codes)
                    continue
                try:
                    d = self.sess(i).get('%s/xcx/enroll_web/v1/pc_login' % API,
                                         params={'code': codes[i]['code'], 'source': 'h5'},
                                         headers=LOGIN_HEADERS, timeout=8).json()
                except Exception:
                    continue
                if str(d.get('msg') or '') != 'ok':
                    continue
                tok = (d.get('data') or {}).get('access_token')
                if not tok:
                    continue
                self.tokens[i] = tok
                ui = userinfo(self.sess(i), tok) or {}
                nick, who = who_of(ui)
                self.nicks[i] = nick
                if who:
                    self.whos[i] = who
                self.cache[self.people[i]['name']] = {
                    'tok': tok, 'nick': nick, 'who': who,
                    'sid': self.people[i]['sid'], 't': time.time()}
                self.state(i, '已就绪 · ' + (nick or ''), '#2e7d32')
                self.emit('scanned', (i, nick))
                self.log('%s 扫码成功（账号：%s）' % (self.people[i]['name'], nick or '?'))
                # 第一次扫码顺便把字段名写进日志，方便确认这接口到底给不给 uid
                if not getattr(self, '_ui_keys', False):
                    self._ui_keys = True
                    self.log('  账号字段：%s' % ', '.join(sorted(ui.keys())[:16]))
                self.say_dup(i, self.people[i]['name'])
                todo.remove(i)
            last_tick = time.time()
            if todo:
                left = deadline - time.time()
                if 0 < left < 120 and int(left) % 60 == 0:
                    self.log('  还能等 %d 分钟' % (left // 60 + 1))
                time.sleep(0.45)

        for i in todo:
            self.state(i, '没扫上', '#c62828')
            self.log('%s 一直没扫码，这次不报' % self.people[i]['name'])
        if self.tokens:
            self.log('扫码完成：%d 人就绪' % len(self.tokens))

    def _scan_one(self, i, p):
        """返回 ('ok', token) / ('skip', None) / ('fail', None)"""
        name, sid = p['name'], p['sid']
        s = self.sess(i)

        # 之前扫过、姓名没改的话直接复用，不再让人重扫一遍。
        # key 用姓名而不是学号：学号/学院是提交时才填的内容，改它们不该
        # 逼人重扫（用户 2026-11-06 明确要求：扫完一次就能改信息再来）。
        ck = name
        hit = self.cache.get(ck)
        if hit and hit.get('tok'):
            if self._token_alive(s, hit['tok']):
                self.tokens[i] = hit['tok']
                self.nicks[i] = hit.get('nick') or ''
                who = hit.get('who') or ''
                if not who:
                    who = who_of(userinfo(s, hit['tok']))[1]
                    hit['who'] = who
                if who:
                    self.whos[i] = who
                self.state(i, '已就绪' + ((' · ' + self.nicks[i]) if self.nicks[i] else ''), '#2e7d32')
                self.log('%s 上次已经扫过了（账号：%s），这次不用再扫'
                         % (name, hit.get('nick') or '已登录'))
                self.say_dup(i, name)
                return 'ok', hit['tok']
            self.cache.pop(ck, None)
            self.log('%s 上次的登录态失效了，需要重新扫一次' % name)

        for rnd in range(1, 7):          # 每人最多出 6 张码：慢一点也来得及
            if self.should_skip(i):
                self.log('%s 被跳过' % name)
                self.state(i, '已跳过', '#757575')
                return 'skip', None
            self.state(i, '出码中…', '#1565c0')
            try:
                j = s.get('%s/xcx/enroll_web/v1/pc_code' % API,
                          headers=LOGIN_HEADERS, timeout=10).json()
            except Exception as e:
                self.log('%s 取码失败：%s' % (name, e))
                time.sleep(2)
                continue
            if j.get('msg') != 'ok':
                self.log('%s 取码失败：%s' % (name, json.dumps(j, ensure_ascii=False)[:120]))
                time.sleep(1)
                continue

            data = j.get('data') or {}
            code = data.get('code')
            qr = data.get('qrcode') or ''
            if not code:
                continue
            raw = base64.b64decode(qr.split(',', 1)[-1] if ',' in qr else qr)

            try:
                img = qr_image(raw, name, sid, i + 1, len(self.people))
                self.emit('qr', (i, img))
            except Exception as e:
                self.log('画二维码失败：%s' % e)
                self.emit('qr', (i, Image.open(io.BytesIO(raw))))

            self.state(i, '等待扫码…', '#ef6c00')
            self.log('现在请 %s（%s）扫码（这张码 8 分钟内有效，扫完自动进入下一位）'
                     % (name, sid))

            deadline = time.time() + 480          # 单张码等 8 分钟，别催命
            told = 0
            broke = False
            while time.time() < deadline:
                if self.stop.is_set():
                    return 'fail', None
                left = deadline - time.time()
                if left < 120 and told != int(left // 60) + 1:
                    told = int(left // 60) + 1
                    self.log('  还有 %d 分钟，二维码就在右边' % told)
                if self.should_skip(i):
                    self.log('%s 被跳过' % name)
                    self.state(i, '已跳过', '#757575')
                    return 'skip', None
                time.sleep(1.2)
                try:
                    d = s.get('%s/xcx/enroll_web/v1/pc_login' % API,
                              params={'code': code, 'source': 'h5'},
                              headers=LOGIN_HEADERS, timeout=8).json()
                except Exception:
                    continue
                m = str(d.get('msg') or '')
                if m == 'ok':
                    tok = (d.get('data') or {}).get('access_token')
                    if tok:
                        self.tokens[i] = tok
                        ui = userinfo(s, tok) or {}
                        nick, who = who_of(ui)
                        self.nicks[i] = nick
                        if who:
                            self.whos[i] = who
                        # 存进共享缓存：下次点「开始」不用再扫，
                        # 改学院/偏移也不影响复用。
                        self.cache[name] = {'tok': tok, 'nick': nick, 'who': who,
                                            'sid': sid, 't': time.time()}
                        self.state(i, '已就绪' + ((' · ' + nick) if nick else ''), '#2e7d32')
                        self.log('%s 扫码成功%s' % (name, ('（账号：%s）' % nick) if nick else ''))
                        # 同一个微信扫了两个人：一个活动只能报一次，后扫的必定
                        # 回「活动期间最多可提交1次」。扫完立刻说清楚，别等开火
                        # 才发现。判重优先用 uid，找不到才退回昵称。
                        self.say_dup(i, name)
                        return 'ok', tok
                if any(k in m for k in ('失效', '过期', 'expire', 'expired')):
                    self.log('%s 那张码过期了，换一张' % name)
                    broke = True
                    break
            if not broke:
                self.log('%s 等太久没扫，换一张码' % name)
            self.state(i, '重新出码…', '#ef6c00')
        self.state(i, '扫码失败', '#c62828')
        self.log('!! %s 一直没扫成功，停止' % name)
        self.emit('alert', '%s 扫码没完成。请他重试，或把他删掉后重新开始' % name)
        return 'fail', None

    def _resolve_eid(self, i, p):
        items = history(self.sess(i), self.tokens.get(i))
        now = time.time()
        cands = [it for it in items if it.get('status') == 1
                 or it.get('start_time', 0) > now - 3600]
        if self.keyword:
            cands = [it for it in cands if self.keyword in (it.get('title') or '')]
        cands.sort(key=lambda x: x.get('start_time', 0))
        if not cands:
            self.log('%s 的账号里看不到候选活动' % p['name'])
            return ''
        for it in cands[:8]:
            self.log('  候选 eid=%s  %s  %s' % (
                it.get('eid'),
                datetime.fromtimestamp(it.get('start_time', 0)).strftime('%m-%d %H:%M'),
                it.get('title')))
        up = [it for it in cands if it.get('start_time', 0) > now - 600]
        pick = up[0] if up else cands[0]
        self.log('自动选中：%s' % pick.get('title'))
        self.emit('eid', pick.get('eid'))
        return pick.get('eid')

    # ---------------- 开火前连接预热
    def _warmup_connections(self):
        """给每个账号的 Session 把到提交接口的 TLS/TCP 连接提前建好。

        原理：requests.Session 的连接池在第一次访问某 host 时才做 TLS 握手。
        前面的 fetch_fields 走的是 GET（短连接、不同路径），提交用的 POST
        到 enroll/v5/enroll 那一刻才第一次碰这个 host，冷启动要 1 秒多。
        这里发一个轻量 HEAD 到提交接口同域，把握手做掉；真正的 POST 就能
        复用连接池里的热连接，把首枪从 1.6 秒压回几百毫秒。
        """
        for i in self.infos:
            if self.stop.is_set():
                return
            try:
                s = self.sess(i)
                s.head(API + '/xcx/enroll/v1/short_detail',
                       timeout=3, params={'eid': self.eid, 'referer': '', 'spider': 'h5'})
            except Exception:
                pass

    # ---------------- 单人开火
    def _fire_one(self, i, p, tok, info, fire_at, results):
        if self.dry:
            results[i] = (True, '演练：不真提交')
            self.state(i, '演练通过', '#2e7d32')
            self.log('[演练] %s 将提交 %s' % (p['name'], json.dumps(info, ensure_ascii=False)))
            return
        # 本人的发射时刻：每轮都重新读一次，
        # 这样运行中双击改「发射秒数」立刻生效，不用重新扫码。
        while True:
            t0 = fire_at + person_delay(p)
            left = t0 - time.time()
            if left <= 0:
                break
            if self.stop.is_set():
                results[i] = (False, '已手动停止')
                return
            time.sleep(min(0.002, left))
        s = self.sess(i)
        n, last = 0, None
        refreshed = False
        deadline = t0 + self.window
        while time.time() < deadline:
            if self.stop.is_set():
                results[i] = (False, '已手动停止')
                return
            n += 1
            payload = {'access_token': tok, 'eid': self.eid, 'info': info,
                       'on_behalf': 0, 'items': [], 'referer': '',
                       'from': 'h5', 'vip_role': 'no', '_a': h5_sign(self.eid)}
            t_send = time.time()
            try:
                r = s.post('%s/xcx/enroll/v5/enroll' % API, headers=SUBMIT_HEADERS,
                           data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                           timeout=5)
                j = r.json()
                t_recv = time.time()
            except Exception as e:
                j = {'msg': 'EXC:%s' % e}
                t_recv = time.time()
            msg = str(j.get('msg', ''))
            if n <= 3 or msg != last:
                self.log('  %s #%d %r  +%.0fms/%.0fms'
                         % (p['name'], n, msg, (t_send - t0) * 1000,
                            (t_recv - t_send) * 1000))
            last = msg
            if msg in OK_MSGS:
                results[i] = (True, msg or '已提交')
                self.state(i, '报名成功', '#2e7d32')
                return
            # 「invalid parameter」不一定是参数真错。活动开放前，创建者账号能
            # 拿到「预览字段」，字段 id 和正式表单对不上，提交就会被判参数非法
            # （2026-11-06 实测：有同学用活动创建者的微信，21:12 就拿到
            #  4 项预览字段，21:14 提交回 invalid parameter；同场的普通账号
            #  同场普通账号开放后取字段，一次成功）。
            # 所以第一次遇到先重新读一次字段、换新内容立刻补一枪，别直接判死。
            if (not refreshed) and msg and 'invalid parameter' in msg.lower():
                refreshed = True
                self.log('  %s：字段可能已变化，重新读取后补发' % p['name'])
                # 第一次就被判参数非法，必须把「服务器原话」和「我到底提交了
                # 什么」都留下来。否则只知道失败了、不知道为什么，等于白抢。
                try:
                    self.log('    服务器原文：%s' % json.dumps(j, ensure_ascii=False)[:300])
                    self.log('    本次提交：%s'
                             % json.dumps(info, ensure_ascii=False)[:400])
                except Exception:
                    pass
                try:
                    reqs = fetch_fields(self.sess(i), tok, self.eid)
                    info2, _un = build_info(reqs, self.people[i]) if reqs else (None, [])
                except Exception:
                    info2 = None
                if info2:
                    info = info2
                    self.infos[i] = info2
                    self.log('  %s：重取到 %d 项字段，等限流窗口过了再发'
                             % (p['name'], len(info2)))
                    if _un:
                        self.log('    !! 重取后仍未匹配（留空）：%s'
                                 % '、'.join(_un[:6]))
                else:
                    self.log('  %s：字段重取为空，仍按原内容重试' % p['name'])
                # 服务器限流窗口约 2.8 秒。上一版在这里直接 continue，271ms 后
                # 就重发，必然吃「请求过于频繁」，白等一轮（2026-11-06 21:40 实测
                # 有位同学因此晚了 4 秒才进）。这里把它等够再发。
                wait = 2.9 - (time.time() - t_send)
                if wait > 0:
                    time.sleep(wait)
                continue
            if msg in DEAD_MSGS:
                results[i] = (False, msg)
                self.state(i, '未成功', '#c62828')
                # 把服务器原文留下来：invalid parameter 这类错误只回一句
                # 笼统的话，日志里没有原文就查不出到底哪个字段不对。
                try:
                    raw = json.dumps(j, ensure_ascii=False)
                except Exception:
                    raw = str(j)
                self.log('    %s 服务器原文：%s' % (p['name'], raw[:400]))
                try:
                    self.log('    %s 本次提交 %d 个字段：%s'
                             % (p['name'], len(info),
                                json.dumps(info, ensure_ascii=False)[:350]))
                except Exception:
                    pass
                return
            if any(k in msg for k in HARD_STOP_KEYS):
                results[i] = (False, msg)
                self.state(i, '登录失效', '#c62828')
                return
            # 射早了（还没开放）→ 先快补一枪，但必须逐步退避。
            # 否则「未开始」会被当成失败猛刷，几秒钟就把限流额度烧光，
            # 等真正开放时反而打不进去。
            if any(k in msg for k in ('未开始', '未开放', '还没开始')):
                time.sleep(min(0.45 * (1.7 ** min(n, 5)), 3.0))
            else:
                time.sleep(self.gap)
        results[i] = (False, last or '超时')
        self.state(i, '未成功', '#c62828')


# ============================================================ 界面
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.engine = None
        self.people = []
        self.photo = None
        self.skipped = set()
        self.cur = None
        # 扫过的登录态（姓名 -> {tok, nick, sid}）。跨「开始」保留，
        # 关掉程序就没了（不落盘，跟原来的安全约定一致）。
        self.token_cache = {}

        root.title('%s%s  %s' % (APP_NAME, '（单人版）' if SINGLE_MODE else '', APP_VER))
        # 右边每人是「一张卡 + 一个 200px 的码」，980 宽时右栏只剩 160 多像素，
        # 码的右边会被窗口切掉（2026-11-06 截图实测）。改成按屏幕算宽一点：
        # 1707x960 的屏上开到 1264x800，左表格 + 右卡片都能完整放下。
        _sw = root.winfo_screenwidth()
        _sh = root.winfo_screenheight()
        _w = min(1264, max(880, _sw - 40))
        _h = min(876, max(640, _sh - 84))
        root.geometry('%dx%d+%d+%d' % (_w, _h, max(0, (_sw - _w) // 2),
                                      max(0, (_sh - _h) // 3)))
        root.minsize(880, 640)
        try:
            root.iconbitmap(default='')
        except Exception:
            pass

        self.style = ttk.Style()
        try:
            self.style.theme_use('vista')
        except Exception:
            pass
        self.style.configure('Treeview', rowheight=26, font=('Microsoft YaHei UI', 10))
        self.style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'))

        self.lic = None
        self.skip_flags = {}
        self.check_license()
        root.after(100, self.pump)

    # ---------------- 授权
    def max_people_allowed(self):
        """名单最多几个人 —— 由授权文件决定，光改程序没有用。

        授权里写了 n=1，这就是单人版；没有 n 字段的（本机白名单、
        或早期授权）按 99 算。客户拿到的授权一律签 n=1，
        就算他把程序里的常量改掉，人数还是被授权卡住。
        """
        p = (self.lic or {}).get('payload') or {}
        try:
            n = int(p.get('n') or 0)
        except Exception:
            n = 0
        if n > 0:
            return n
        return 1 if SINGLE_MODE else 99

    def strict_licensing(self):
        """这份授权是不是「客户授权」（带人数限制 n）。

        是 → 开火前再验一次授权；不是（本机白名单）→ 完全不额外校验，
        行为和加这个功能之前一样。
        """
        p = (self.lic or {}).get('payload') or {}
        try:
            return int(p.get('n') or 0) > 0
        except Exception:
            return False

    def check_license(self):
        try:
            import auth_core
            self.lic = auth_core.check()
        except Exception as e:
            self.lic = {'ok': False, 'reason': '授权模块读取失败：%s' % e, 'mid': '未知'}
        if self.lic.get('ok'):
            self.build_main()
        else:
            self.build_license()

    def build_license(self):
        for w in self.root.winfo_children():
            w.destroy()
        f = ttk.Frame(self.root, padding=28)
        f.pack(fill='both', expand=True)

        ttk.Label(f, text='这个程序需要授权', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(f, text=self.lic.get('reason', ''), foreground='#c62828',
                  font=('Microsoft YaHei UI', 11)).pack(anchor='w', pady=(6, 18))

        box = ttk.LabelFrame(f, text=' 把下面这个机器码发给发程序给你的人 ', padding=14)
        box.pack(fill='x')
        mid = self.lic.get('mid', '')
        self.mid_var = tk.StringVar(value=mid)
        e = ttk.Entry(box, textvariable=self.mid_var, font=('Consolas', 15),
                      state='readonly', width=28)
        e.pack(side='left')
        ttk.Button(box, text='复制', width=8,
                   command=lambda: (self.root.clipboard_clear(),
                                    self.root.clipboard_append(mid),
                                    messagebox.showinfo(APP_NAME, '机器码已复制'))).pack(side='left', padx=8)

        ttk.Label(f, text='对方会回给你一个 license.txt，把它放到本程序所在文件夹里，\n'
                          '然后点下面的「我已拿到授权」即可。',
                  font=('Microsoft YaHei UI', 10), foreground='#444').pack(anchor='w', pady=18)

        row = ttk.Frame(f)
        row.pack(anchor='w')
        ttk.Button(row, text='我已拿到授权，重新检查', command=self.recheck).pack(side='left')
        ttk.Button(row, text='打开程序所在文件夹', command=self.open_dir).pack(side='left', padx=8)

        self.write_request(mid)

    def write_request(self, mid):
        try:
            p = os.path.join(HERE, '授权申请.txt')
            with open(p, 'w', encoding='utf-8') as f:
                f.write('程序名：%s\n机器码：%s\n申请时间：%s\n'
                        % (APP_NAME, mid, time.strftime('%Y-%m-%d %H:%M:%S')))
        except Exception:
            pass

    def recheck(self):
        try:
            import auth_core
            import importlib
            importlib.reload(auth_core)
            info = auth_core.check()
        except Exception as e:
            info = {'ok': False, 'reason': str(e)}
        if info.get('ok'):
            self.lic = info
            self.build_main()
        else:
            self.lic = info
            messagebox.showwarning(APP_NAME, '还是不行：%s' % info.get('reason', ''))

    def open_dir(self):
        try:
            os.startfile(HERE)
        except Exception:
            pass

    # ---------------- 识别活动码（普通二维码 → 自动填 eid）
    def pick_qr(self):
        """
        点「识别活动码」的入口。

        包一层 try/except：以前窗口里任何一个控件出问题（比如打包后
        ImageGrab 起不来），异常只会被 Tk 悄悄吞掉，界面一动不动，
        看起来就是「按钮点不了」（2026-11-06 22:08 用户反馈）。
        现在至少会弹一个框说清楚哪一步坏了。
        """
        try:
            self._pick_qr_ui()
        except Exception as e:
            try:
                self.log_ui('!! 识别活动码没打开：%s' % e)
            except Exception:
                pass
            messagebox.showerror(
                '识别活动码',
                '这个窗口没能打开：%s\n\n'
                '不影响用：直接把链接粘到上面「活动号(eid)」框里，'
                '点「开始」时会自动抠出 eid。' % e,
                parent=self.root)

    def _pick_qr_ui(self):
        """
        弹一个粘贴框，用户把活动二维码截图 Ctrl+V 贴进来，
        程序解出里面的 eid 自动填到「活动号」。
        """
        from PIL import ImageGrab

        dlg = tk.Toplevel(self.root)
        dlg.title('识别活动码')
        # 每次打开都清掉上一次的图，免得拿旧图误识别
        self._qr_pil = None
        dlg.transient(self.root)
        dlg.resizable(False, False)
        pad = ttk.Frame(dlg, padding=14)
        pad.pack(fill='both', expand=True)
        ttk.Label(pad, text='方式一：粘贴链接（推荐，最快）',
                  font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(pad,
                  text='微信扫小程序码进活动页 → 右上角「…」→ 复制链接 → 回这里按 Ctrl+V，\n'
                       '程序会自动从链接里抠出 eid。别人发来的报名链接也能直接粘。',
                  foreground='#555').pack(anchor='w', pady=(2, 5))
        self.qr_url_var = tk.StringVar(value='')
        row = ttk.Frame(pad)
        row.pack(fill='x')
        ttk.Entry(row, textvariable=self.qr_url_var, width=50,
                  font=('Consolas', 9)).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='从剪贴板取', width=12,
                   command=self._clip_to_url).pack(side='left', padx=(6, 0))

        ttk.Label(pad, text='方式二：粘贴普通二维码截图',
                  font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w', pady=(12, 2))
        ttk.Label(pad,
                  text='只认标准二维码。微信「小程序码 / 太阳码」是私有格式，第三方解不了，\n'
                       '那种情况请走上面的方式一。',
                  foreground='#777').pack(anchor='w', pady=(0, 6))

        self.qr_preview = ttk.Label(pad, text='（还没有粘贴图片）',
                                    foreground='#999', anchor='center',
                                    relief='solid', width=46, padding=12)
        self.qr_preview.pack(fill='x')
        btn = ttk.Frame(pad)
        btn.pack(fill='x', pady=(12, 0))
        ttk.Button(btn, text='识别并填入', command=lambda: self._decode_paste(dlg)).pack(side='left')
        ttk.Button(btn, text='取消', command=dlg.destroy).pack(side='left', padx=8)
        dlg.bind('<Control-v>', lambda e: self._paste_qr_img())
        dlg.bind('<Control-V>', lambda e: self._paste_qr_img())
        dlg.focus_force()
        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        dlg.geometry('+%d+%d' % (max(0, x), max(0, y)))
        # 提到最前面：不这么做时，主窗口稍大一点就会把这个小框盖在下面，
        # 用户点了按钮却「什么都没发生」。
        try:
            dlg.lift()
            dlg.attributes('-topmost', True)
            dlg.after(400, lambda: dlg.attributes('-topmost', False))
            dlg.grab_set()          # 模态，点主窗口不会把它压到后面
            dlg.focus_force()
        except Exception:
            pass

    def _dlg_of(self):
        w = getattr(self, 'qr_preview', None)
        try:
            return w.winfo_toplevel() if w is not None else self.root
        except Exception:
            return self.root

    def _clip_to_url(self):
        """把剪贴板里的链接/分享文案读进输入框。"""
        try:
            txt = (self.root.clipboard_get() or '').strip()
        except Exception:
            txt = ''
        if not txt:
            messagebox.showinfo('识别活动码',
                                '剪贴板里没有文字。\n\n'
                                '请先在微信里点「…」→「复制链接」，再来点这个按钮。',
                                parent=self._dlg_of())
            return
        self.qr_url_var.set(txt)
        eid = extract_eid(txt)
        if eid:
            self.log_ui('已从剪贴板里认出 eid = %s' % eid)

    def _apply_eid(self, eid, dlg=None, how='已识别'):
        self.eid_var.set(eid)
        try:
            self.log_ui('%s eid = %s，自动填入' % (how, eid))
        except Exception:
            pass
        if dlg is not None:
            try:
                dlg.destroy()
            except Exception:
                pass
        messagebox.showinfo('识别活动码', '已填入活动号(eid)：\n\n%s' % eid,
                            parent=self.root)

    def _paste_qr_img(self):
        # 剪贴板里可能是文字（微信「复制链接」），也可能是图片，先看文字
        try:
            txt = (self.root.clipboard_get() or '').strip()
        except Exception:
            txt = ''
        if txt and not txt.lower().startswith('data:image'):
            self.qr_url_var.set(txt)
            eid = extract_eid(txt)
            if eid:
                self._apply_eid(eid, self._dlg_of(), '已从剪贴板链接识别')
                return
        try:
            from PIL import ImageGrab
            img = ImageGrab.grabclipboard()
            if img is None:
                messagebox.showinfo('识别活动码',
                                    '剪贴板里既没有链接，也没有图片。\n\n'
                                    '最省事的做法：微信扫小程序码进活动页 → 右上角「…」\n'
                                    '→ 复制链接 → 回来按 Ctrl+V。',
                                    parent=self._dlg_of())
                return
            img = img.convert('RGB')
            img.thumbnail((360, 360))
            self._qr_pil = img
            self._qr_photo = ImageTk.PhotoImage(img)
            self.qr_preview.configure(image=self._qr_photo, text='')
        except Exception as e:
            messagebox.showerror('识别活动码', '读取剪贴板失败：%s' % e,
                                 parent=self._dlg_of())

    def _decode_paste(self, dlg):
        # 输入框里有链接 → 优先按链接解析
        txt = ''
        try:
            txt = (self.qr_url_var.get() or '').strip()
        except Exception:
            pass
        if txt:
            eid = extract_eid(txt)
            if eid:
                self._apply_eid(eid, dlg, '已从粘贴内容识别')
                return
            messagebox.showinfo('识别活动码',
                                '这段内容里没找到活动号(eid)。\n\n'
                                '内容：%s\n\n'
                                '正确的链接长这样：\n'
                                'http://m.baominggongju.com/?eid=6aedb8d2c0fe767108df14b6'
                                % txt[:160],
                                parent=dlg)
            return

        img = getattr(self, '_qr_pil', None)
        if img is None:
            messagebox.showinfo('识别活动码',
                                '先用「方式一」粘贴链接，或按 Ctrl+V 粘二维码截图',
                                parent=dlg)
            return
        text = decode_qr_text(img)
        if not text:
            messagebox.showwarning('识别活动码',
                                   '这张图里没解出二维码。\n\n'
                                   '如果是微信「小程序码 / 太阳码」，那是微信私有格式，\n'
                                   '第三方确实解不了 —— 请改用方式一：\n'
                                   '微信扫它进活动页 → 右上角「…」→ 复制链接 → 粘到上面。\n\n'
                                   '如果是普通二维码，请确认截图清晰、四角完整、没被水印遮住。',
                                   parent=dlg)
            return
        eid = extract_eid(text)
        if not eid:
            messagebox.showinfo('识别活动码',
                                '解出了内容，但里面没有活动号(eid)。\n\n内容：%s\n\n'
                                '如果这是报名链接，请把整条链接复制给我。' % text[:120],
                                parent=dlg)
            return
        self._apply_eid(eid, dlg, '已从二维码识别')

    # ---------------- 主界面
    def build_main(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.skip_flags = {}
        self.scanned_ids = set()

        # 顶部
        top = ttk.Frame(self.root, padding=(14, 10, 14, 4))
        top.pack(fill='x')
        ttk.Label(top, text=APP_NAME, font=('Microsoft YaHei UI', 15, 'bold')).pack(side='left')
        ttk.Label(top, text=APP_VER, foreground='#888',
                  font=('Microsoft YaHei UI', 9)).pack(side='left', padx=(8, 0))
        left = max(0, self.lic.get('left_sec') or 0)
        if left >= 86400 * 365 * 20:          # 20 年以上按永久显示
            lic_txt = '永久授权'
        elif left >= 86400:
            lic_txt = '授权剩余 %d 天' % (left // 86400)
        else:
            lic_txt = '授权剩余 %.1f 小时' % (left / 3600.0)
        ttk.Label(top, text=lic_txt,
                  foreground='#2e7d32' if left > 3600 else '#c62828',
                  font=('Microsoft YaHei UI', 10)).pack(side='right')

        # 活动行
        act = ttk.LabelFrame(self.root, text=' 活动 ', padding=10)
        act.pack(fill='x', padx=14, pady=6)
        r1 = ttk.Frame(act)
        r1.pack(fill='x')
        ttk.Label(r1, text='活动号(eid)').pack(side='left')
        self.eid_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.eid_var, width=30, font=('Consolas', 10)).pack(side='left', padx=6)
        ttk.Button(r1, text='识别活动码', width=10,
                   command=self.pick_qr).pack(side='left')
        ttk.Label(r1, text='关键词').pack(side='left', padx=(10, 0))
        self.kw_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.kw_var, width=22).pack(side='left', padx=6)
        ttk.Label(r1, text='（eid 可留空，扫完第一个人自动找）',
                  foreground='#777').pack(side='left')

        # 时机控制：默认值就是最优，只有出问题才需要动
        r2 = ttk.Frame(act)
        r2.pack(fill='x', pady=(8, 0))
        self.delay_var = tk.StringVar(value='0')
        self.gap_var = tk.StringVar(value='3')
        self.win_var = tk.StringVar(value='45')
        ttk.Label(r2, text='统一发射时刻').pack(side='left')
        ttk.Spinbox(r2, textvariable=self.delay_var, from_=-2.0, to=30.0, increment=0.1,
                    width=6).pack(side='left', padx=(4, 2))
        ttk.Label(r2, text='秒').pack(side='left')
        ttk.Label(r2, text='（0=准时  正=推迟  负=提前；个人可在下表单独调）',
                  foreground='#777').pack(side='left', padx=(2, 18))
        ttk.Label(r2, text='失败重试间隔').pack(side='left')
        ttk.Spinbox(r2, textvariable=self.gap_var, from_=0.5, to=30.0, increment=0.5,
                    width=5).pack(side='left', padx=(4, 2))
        ttk.Label(r2, text='秒').pack(side='left')
        ttk.Label(r2, text='最多等').pack(side='left', padx=(18, 0))
        ttk.Spinbox(r2, textvariable=self.win_var, from_=5, to=300, increment=5,
                    width=5).pack(side='left', padx=(4, 2))
        ttk.Label(r2, text='秒').pack(side='left')

        self.act_lbl = ttk.Label(act, text='尚未读取活动', font=('Microsoft YaHei UI', 10),
                                 foreground='#333')
        self.act_lbl.pack(anchor='w', pady=(8, 0))
        self.cd_lbl = ttk.Label(act, text='', font=('Consolas', 20, 'bold'), foreground='#1565c0')
        self.cd_lbl.pack(anchor='w')

        # 主体（最后 pack，先让底部按钮和日志占到固定高度，否则会被挤扁）
        body = ttk.Frame(self.root)

        lf = ttk.LabelFrame(body, text=' 参与的人 ', padding=8)
        lf.pack(side='left', fill='both', expand=False)
        cols = ('name', 'sid', 'college', 'fire', 'state')
        self.tree = ttk.Treeview(lf, columns=cols, show='headings', height=9)
        for c, t, w, a in (('name', '姓名', 78, 'w'), ('sid', '学号', 96, 'w'),
                           ('college', '学院', 100, 'w'),
                           ('fire', '发射秒数', 74, 'center'), ('state', '状态', 104, 'w')):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        self.tree.bind('<Double-1>', self.on_tree_dbl)

        # 按钮行必须先占位（side=bottom），否则表格 expand 会把它挤成 1x1
        bar = ttk.Frame(lf)
        bar.pack(side='bottom', fill='x', pady=(8, 0))
        self.person_btns = []
        b = ttk.Button(bar, text='＋ 添加人', width=11, command=self.add_person)
        b.pack(side='left')
        self.person_btns.append(b)
        b = ttk.Button(bar, text='修改', width=6, command=self.edit_person)
        b.pack(side='left', padx=4)
        self.person_btns.append(b)
        b = ttk.Button(bar, text='删除', width=6, command=self.del_person)
        b.pack(side='left')
        self.person_btns.append(b)

        ttk.Label(lf, text='双击「发射秒数」可单独改这个人',
                  foreground='#777').pack(side='bottom', anchor='w', pady=(4, 0))
        self.tree.pack(fill='both', expand=True)

        # 右：每人一张卡，勾上的才报名；码一次全部出来，扫哪张都行
        rf = ttk.LabelFrame(body, text=' 选人扫码（勾上的才报名 · 扫哪张都行） ', padding=6)
        rf.pack(side='left', fill='both', expand=True, padx=(10, 0))
        wrap = ttk.Frame(rf)
        wrap.pack(fill='both', expand=True)
        self.qr_canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(wrap, orient='vertical', command=self.qr_canvas.yview)
        self.qr_canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.qr_canvas.pack(side='left', fill='both', expand=True)
        self.qr_holder = ttk.Frame(self.qr_canvas)
        self._qr_win = self.qr_canvas.create_window((0, 0), window=self.qr_holder,
                                                    anchor='nw')
        self.qr_holder.bind('<Configure>',
                            lambda e: self.qr_canvas.configure(
                                scrollregion=self.qr_canvas.bbox('all')))
        # 卡片区宽度跟着可视宽度走，否则两列挤在左边、右边留一大片空白
        self.qr_canvas.bind('<Configure>',
                            lambda e: self.qr_canvas.itemconfigure(
                                self._qr_win, width=e.width))
        self.qr_imgs, self.qr_status, self.pick_vars, self.qr_photos = {}, {}, {}, {}
        self.build_qr_cards()

        # 底部按钮
        bot = ttk.Frame(self.root, padding=(14, 4))
        self.dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bot, text='演练（不真正提交）', variable=self.dry_var).pack(side='left')
        self.go_btn = ttk.Button(bot, text='开始', command=self.start, width=12)
        self.go_btn.pack(side='left', padx=10)
        self.stop_btn = ttk.Button(bot, text='停止', command=self.halt, width=8, state='disabled')
        self.stop_btn.pack(side='left')
        self.skip_btn = ttk.Button(bot, text='没扫完也继续', command=self.skip_current,
                                   width=12, state='disabled')
        self.skip_btn.pack(side='left', padx=8)
        ttk.Button(bot, text='打开程序文件夹', command=self.open_dir).pack(side='right')

        # 日志
        lg = ttk.LabelFrame(self.root, text=' 日志 ', padding=6)
        lg.pack(side='bottom', fill='both', expand=False, padx=14, pady=(2, 12))
        bot.pack(side='bottom', fill='x')
        # 日志 9 行改 4 行：多出来的高度让右侧二维码卡片一屏能放全，
        # 日志只留最近几条，翻历史可以往上滚
        self.log_txt = tk.Text(lg, height=4, font=('Consolas', 9), wrap='none',
                               bg='#1e1e1e', fg='#d4d4d4', insertbackground='#d4d4d4')
        sb = ttk.Scrollbar(lg, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.log_txt.pack(fill='both', expand=True)
        self.log_txt.configure(state='disabled')

        body.pack(fill='both', expand=True, padx=14, pady=4)

        self.load_people()
        self.refresh_people()

    def skip_current(self):
        """并行扫码后没有「当前这个人」了：点一次 = 还没扫的都跳过，直接进下一步。"""
        done = getattr(self, 'scanned_ids', set())
        n = 0
        for i in range(len(self.people)):
            if i in done or self.skip_flags.get(i):
                continue
            self.skip_flags[i] = True
            n += 1
        if n:
            self.log_ui('已跳过还没扫码的 %d 人，继续往下走' % n)
        else:
            self.log_ui('没有可跳过的人')

    # ---------------- 人员
    def load_people(self):
        try:
            if os.path.exists(PEOPLE_PATH):
                d = json.load(open(PEOPLE_PATH, encoding='utf-8'))
                self.people = [p for p in (d.get('people') or [])
                               if isinstance(p, dict) and p.get('name')]
                # 不管配置文件里存了几个，都不能超过授权允许的人数
                limit = self.max_people_allowed()
                if len(self.people) > limit:
                    self.people = self.people[:limit]
                if not self.eid_var.get():
                    raw = d.get('eid') or ''
                    # 文件里可能存着 "?eid=xxx" 这种脏值，能修就修
                    self.eid_var.set(clean_eid(raw) or raw)
                    self.kw_var.set(d.get('keyword') or '')
                if hasattr(self, 'delay_var'):
                    self.delay_var.set(str(d.get('delay', '0')))
                    self.gap_var.set(str(d.get('gap', '3')))
                    self.win_var.set(str(d.get('window', '45')))
        except Exception:
            self.people = []

    def save_people(self):
        try:
            d = {'eid': self.eid_var.get().strip() if hasattr(self, 'eid_var') else '',
                 'keyword': self.kw_var.get().strip() if hasattr(self, 'kw_var') else '',
                 'people': self.people}
            if hasattr(self, 'delay_var'):
                d['delay'] = self.delay_var.get().strip()
                d['gap'] = self.gap_var.get().strip()
                d['window'] = self.win_var.get().strip()
            json.dump(d, open(PEOPLE_PATH, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    def refresh_people(self):
        if not hasattr(self, 'tree'):
            return
        self.tree.delete(*self.tree.get_children())
        for p in self.people:
            self.tree.insert('', 'end', values=(
                p.get('name', ''), p.get('sid', ''), p.get('college', ''),
                '%.1f' % person_delay(p), '未开始'))
        try:
            self.build_qr_cards()
        except Exception:
            pass

    def build_qr_cards(self):
        """右侧每人一张卡：勾选框 + 姓名学号 + 二维码 + 状态。"""
        holder = getattr(self, 'qr_holder', None)
        if holder is None:
            return
        for w in holder.winfo_children():
            w.destroy()
        self.qr_imgs, self.qr_status, self.pick_vars = {}, {}, {}
        if not hasattr(self, 'qr_photos'):
            self.qr_photos = {}

        if not self.people:
            ttk.Label(holder,
                      text='还没有人\n\n先点左边「＋ 添加人」\n把姓名 / 学号 / 学院填进来',
                      font=('Microsoft YaHei UI', 12), foreground='#666',
                      anchor='center', justify='center').pack(pady=40)
            return

        # 两列网格：一个人一张卡纵向排会堆到屏幕外，4 个人就要滚动才能看到
        # 第 3、4 个码。排两列后 4 个人两行看全，扫谁点谁（2026-11-06 改）。
        n = len(self.people)
        cols = 2 if n > 1 else 1
        for c in range(cols):
            holder.columnconfigure(c, weight=1, uniform='qrc')

        for i, p in enumerate(self.people):
            r, c = divmod(i, cols)
            card = ttk.LabelFrame(holder, padding=3)
            card.grid(row=r, column=c, sticky='nsew', padx=2, pady=2)
            top = ttk.Frame(card)
            top.pack(fill='x')
            var = tk.BooleanVar(value=not self.skip_flags.get(i, False))
            self.pick_vars[i] = var
            ttk.Checkbutton(top, text='%d. %s' % (i + 1, p.get('name', '')),
                            variable=var).pack(side='left')
            ttk.Label(top, text=p.get('sid', ''), foreground='#777').pack(side='left', padx=(8, 0))
            st = ttk.Label(top, text='未开始', foreground='#888')
            st.pack(side='right')
            self.qr_status[i] = st

            img_lbl = ttk.Label(card, text='点「开始」后出码', anchor='center',
                                foreground='#999')
            img_lbl.pack(pady=(3, 0))
            self.qr_imgs[i] = img_lbl

    def sel_index(self):
        s = self.tree.selection()
        if not s:
            return None
        return self.tree.index(s[0])

    def set_person_btns(self, state):
        for b in getattr(self, 'person_btns', []):
            try:
                b.configure(state=state)
            except Exception:
                pass

    def is_running(self):
        """
        是否正在跑。

        不能用 self.go_btn.cget('state') == 'disabled'：
        ttk 控件的 cget 返回的是 Tcl index 对象，跟字符串比较永远是 False，
        结果运行中的各种保护全部失效（加人删人会串行、改时间也不认）。
        必须用 instate 看状态位。
        """
        try:
            return bool(self.go_btn.instate(['disabled']))
        except Exception:
            return False

    # ---------------- 每人的发射秒数
    def on_tree_dbl(self, e):
        row = self.tree.identify_row(e.y)
        if not row:
            return
        i = self.tree.index(row)
        if self.tree.identify_column(e.x) == '#4':
            self.edit_delay(i)
        else:
            self.edit_person()

    def edit_delay(self, i):
        """双击「发射秒数」这一格，单独改这个人。

        运行中也能改：扫完码、正在等开放的时候改，改完立刻按新秒数发射，
        不用重新扫码（以前一刀切禁用，逼得人重扫，是设计失误）。
        """
        p = self.people[i]
        running = self.is_running()
        cur = person_delay(p)
        v = simpledialog.askfloat(
            '发射秒数 — %s' % p.get('name', ''),
            '开放那一刻往后推多少秒提交？\n\n'
            '  0    = 准时（推荐，默认）\n'
            '  1.5  = 推迟 1.5 秒\n'
            '  -0.3 = 提前 0.3 秒（对时略偏快时用）\n\n'
            '三个人各自独立，互不影响。\n'
            + ('现在是运行中：改完立刻按新值发射，不用重新扫码。' if running else ''),
            initialvalue=cur, minvalue=-30.0, maxvalue=120.0, parent=self.root)
        if v is None:
            return
        p['delay'] = round(float(v), 2)
        self.save_people()
        if running:
            # 只改这一格，不能调 refresh_people —— 那会把状态列刷回「未开始」
            try:
                item = self.tree.get_children()[i]
                vals = list(self.tree.item(item, 'values'))
                vals[3] = '%.1f' % p['delay']
                self.tree.item(item, values=vals)
            except Exception:
                pass
            self.log_ui('%s 的发射秒数改成 %+.1f 秒，已生效（不用重新扫码）'
                        % (p.get('name', ''), p['delay']))
        else:
            self.refresh_people()
            self.tree.selection_set(self.tree.get_children()[i])

    def add_person(self):
        if self.is_running():
            self.log_ui('正在运行，先点「停止」才能加人（发射秒数可以双击直接改，不用停）')
            return
        if len(self.people) >= self.max_people_allowed():
            self.log_ui('这个授权只能填 %d 个人；要换人先选中原有的人点「删除」'
                        % self.max_people_allowed())
            messagebox.showinfo(
                '人数已满',
                '这份授权只能报名 %d 个人。\n\n'
                '要换人：先选中表格里原有的人，点「删除」，'
                '再点「＋ 添加人」。\n'
                '如果是资料填错，直接选中他点「修改」。'
                % self.max_people_allowed(),
                parent=self.root)
            return
        dlg = PersonDialog(self.root, None)
        # 必须等这个窗口关掉再读结果。
        # 少了这句：弹窗刚打开、用户还没打字，这里就先读到空的 result
        # 直接 return 了；等用户填完点「确定」，结果没人接收，表格永远加不进人。
        self.root.wait_window(dlg)
        if dlg.result:
            self.people.append(dlg.result)
            self.save_people()
            self.refresh_people()
            self.log_ui('已添加：%s（学号 %s）' % (dlg.result['name'], dlg.result['sid']))

    def edit_person(self):
        if self.is_running():
            self.log_ui('正在运行，先点「停止」才能改人员信息（发射秒数可以双击直接改，不用停）')
            return
        i = self.sel_index()
        if i is None:
            return
        dlg = PersonDialog(self.root, self.people[i])
        self.root.wait_window(dlg)
        if dlg.result:
            self.people[i] = dlg.result
            self.save_people()
            self.refresh_people()
            self.log_ui('已修改：%s' % dlg.result['name'])

    def del_person(self):
        if self.is_running():
            self.log_ui('正在运行，先点「停止」才能删人')
            return
        i = self.sel_index()
        if i is None:
            return
        if messagebox.askyesno(APP_NAME, '删除「%s」？' % self.people[i]['name']):
            self.people.pop(i)
            self.save_people()
            self.refresh_people()

    # ---------------- 运行
    def start(self):
        if not self.people:
            messagebox.showwarning(APP_NAME, '先添加要报名的人')
            return
        for p in self.people:
            if not p.get('sid') or not p.get('college'):
                messagebox.showwarning(APP_NAME, '「%s」的学号或学院没填' % p.get('name'))
                return
        try:
            delay = float(self.delay_var.get())
            gap = float(self.gap_var.get())
            win = float(self.win_var.get())
        except ValueError:
            messagebox.showwarning(APP_NAME, '发射时刻 / 重试间隔 / 最多等 这几格要填数字')
            return
        if gap < 0.5:
            messagebox.showwarning(APP_NAME, '重试间隔不要小于 0.5 秒，服务器限流窗口约 2.8 秒')
            return
        if abs(delay) > 60:
            messagebox.showwarning(APP_NAME, '发射偏移太大了（-60 ~ 60 秒之间）')
            return

        # 活动号可能被粘成 "?eid=xxx" 或 "xxx&_referer=..."，先规范成纯 eid
        eid_raw = self.eid_var.get().strip()
        eid = clean_eid(eid_raw)
        if eid_raw and not eid:
            messagebox.showwarning(APP_NAME,
                                   '活动号看不懂：\n\n%s\n\n'
                                   '正确写法是 24 位字符，例如：\n'
                                   '6aedb8d2c0fe767108df14b6\n\n'
                                   '也可以留空，程序扫完第一个人会自动找活动。'
                                   % eid_raw[:80])
            return
        if eid != eid_raw:
            self.eid_var.set(eid)
            self.log_ui('活动号已自动规范为 %s' % eid)

        # 勾选框决定谁参战：没勾的当"跳过"，不出码也不提交
        picked = [i for i, v in getattr(self, 'pick_vars', {}).items() if v.get()]
        if not picked:
            messagebox.showwarning(APP_NAME, '一个都没勾选。\n\n至少勾一个人才开始。')
            return
        self.skip_flags = {i: True for i in range(len(self.people)) if i not in picked}

        self.go_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.skip_btn.configure(state='normal')
        self.set_person_btns('disabled')
        self.stop.clear()
        self.scanned_ids = set()
        self.cur = None
        self.save_people()
        self.set_log('')
        self.engine = Engine(list(self.people), eid,
                             self.kw_var.get().strip(), self.dry_var.get(),
                             lambda k, v: self.q.put((k, v)), self.stop,
                             self.skip_flags, delay=delay, gap=gap, window=win,
                             cache=self.token_cache,
                             max_people=self.max_people_allowed(),
                             strict_lic=self.strict_licensing())
        threading.Thread(target=self.engine.run, daemon=True).start()

    def halt(self):
        self.stop.set()
        self.log_ui('（已请求停止）')

    def pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == 'log':
                    self.log_ui(val)
                elif kind == 'qr':
                    self.show_qr(*val)
                elif kind == 'scanned':
                    i, nick = val
                    self.scanned_ids.add(i)
                    self.set_qr_status(i, '已就绪 ' + (nick or ''), '#2e7d32')
                elif kind == 'state':
                    self.set_state(*val)
                elif kind == 'info':
                    self.show_info(val)
                elif kind == 'countdown':
                    self.show_countdown(val)
                elif kind == 'eid':
                    self.eid_var.set(val or '')
                elif kind == 'alert':
                    messagebox.showwarning(APP_NAME, val)
                elif kind == 'done':
                    self.finish(val)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def finish(self, summary):
        self.go_btn.configure(state='normal')
        self.stop_btn.configure(state='disabled')
        self.skip_btn.configure(state='disabled')
        self.set_person_btns('normal')
        if summary:
            messagebox.showinfo(APP_NAME, '完成：成功 %d / %d 人'
                                % (summary.get('ok', 0), summary.get('total', 0)))

    def show_qr(self, i, img):
        """把第 i 个人的二维码画进他自己的卡片里，互不覆盖。"""
        self.cur = i
        lab = getattr(self, 'qr_imgs', {}).get(i)
        if lab is None:
            return
        # 172 而不是 200：两列两行要塞进一屏，多 11px 就会冒出滚动条。
        # 172px 在这个屏上约 2.3cm，手机 20~30cm 外扫得动。
        target = 172
        k = min(target / float(img.width), target / float(img.height), 1.0)
        im = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))),
                        Image.LANCZOS)
        photo = ImageTk.PhotoImage(im)
        self.qr_photos[i] = photo          # 必须留住引用，否则会被回收变白板
        lab.configure(image=photo, text='')

    def set_qr_status(self, i, text, color='#888'):
        st = getattr(self, 'qr_status', {}).get(i)
        if st is not None:
            try:
                st.configure(text=text, foreground=color)
            except Exception:
                pass

    def show_info(self, d):
        t = d.get('title') or '(无标题)'
        st = d.get('start') or 0
        when = datetime.fromtimestamp(st).strftime('%m-%d %H:%M:%S') if st else '未知'
        self.act_lbl.configure(text='%s   |   开放 %s   |   名额 %s'
                               % (t, when, d.get('limit')))

    def show_countdown(self, sec):
        if sec is None:
            return
        if sec <= 0:
            self.cd_lbl.configure(text='开火中…', foreground='#c62828')
            return
        s = int(sec)
        self.cd_lbl.configure(text='距开放 %02d:%02d:%02d' % (s // 3600, s % 3600 // 60, s % 60),
                              foreground='#1565c0' if sec > 60 else '#e65100')

    def set_state(self, i, text, color=''):
        try:
            item = self.tree.get_children()[i]
            vals = list(self.tree.item(item, 'values'))
            vals[4] = text
            self.tree.item(item, values=vals)
            if color:
                self.tree.tag_configure('c%d' % i, foreground=color)
                self.tree.item(item, tags=('c%d' % i,))
        except Exception:
            pass
        # 右侧卡片上的状态也跟着变，不用来回看两处
        self.set_qr_status(i, text, color or '#888')

    def log_ui(self, msg):
        if not hasattr(self, 'log_txt'):
            return
        line = '[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg)
        self.log_txt.configure(state='normal')
        self.log_txt.insert('end', line + '\n')
        # 长时间挂着不能无限长，超过 2000 行就砍掉最前面的 500 行
        try:
            if int(self.log_txt.index('end-1c').split('.')[0]) > 2000:
                self.log_txt.delete('1.0', '500.0')
        except Exception:
            pass
        self.log_txt.see('end')
        self.log_txt.configure(state='disabled')
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def set_log(self, text):
        if not hasattr(self, 'log_txt'):
            return
        self.log_txt.configure(state='normal')
        self.log_txt.delete('1.0', 'end')


class PersonDialog(tk.Toplevel):
    def __init__(self, master, person=None):
        super().__init__(master)
        self.title('人员信息')
        self.result = None
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        f = ttk.Frame(self, padding=16)
        f.pack(fill='both', expand=True)
        self.vars = {}
        rows = [('name', '姓名'), ('sid', '学号'), ('college', '学院全称'), ('phone', '手机号(可空)')]
        self.first_entry = None
        for r, (k, t) in enumerate(rows):
            ttk.Label(f, text=t).grid(row=r, column=0, sticky='e', pady=5)
            v = tk.StringVar(value=(person or {}).get(k, ''))
            e = ttk.Entry(f, textvariable=v, width=32)
            e.grid(row=r, column=1, padx=8, pady=5)
            self.vars[k] = v
            if k == 'name':
                self.first_entry = e
        ttk.Label(f, text='学院要写全称，例如「××学院」', foreground='#777'
                  ).grid(row=len(rows), column=1, sticky='w')

        # 这个人自己的发射秒数（写在人员后面，可随时单独调）
        r = len(rows) + 1
        ttk.Label(f, text='发射秒数').grid(row=r, column=0, sticky='e', pady=5)
        self.delay_var = tk.StringVar(
            value=('%.1f' % person_delay(person)) if person else '0.0')
        ttk.Spinbox(f, textvariable=self.delay_var, from_=-30.0, to=120.0,
                    increment=0.1, width=8).grid(row=r, column=1, sticky='w', padx=8, pady=5)
        ttk.Label(f, text='0=准时开放那一刻提交；1.5=推迟 1.5 秒；-0.3=提前（一般填 0）',
                  foreground='#777').grid(row=r + 1, column=1, sticky='w')

        b = ttk.Frame(f)
        b.grid(row=r + 2, column=0, columnspan=2, pady=(14, 0))
        ttk.Button(b, text='确定', width=10, command=self.ok).pack(side='left', padx=6)
        ttk.Button(b, text='取消', width=10, command=self.destroy).pack(side='left')
        self.bind('<Return>', lambda e: self.ok())
        self.bind('<Escape>', lambda e: self.destroy())
        self.update_idletasks()
        w = self.winfo_reqwidth()
        h = self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry('+%d+%d' % (max(0, x), max(0, y)))
        self.focus_force()
        # 光标直接落在「姓名」框里，免得打字打到别的窗口、框还是空的
        if self.first_entry is not None:
            self.first_entry.focus_set()

    def ok(self):
        d = {k: v.get().strip() for k, v in self.vars.items()}
        if not d['name']:
            messagebox.showwarning('人员信息', '姓名必须填，不填加不进去', parent=self)
            return
        if not d['sid']:
            messagebox.showwarning('人员信息', '学号必须填，不填加不进去', parent=self)
            return
        try:
            d['delay'] = round(float(self.delay_var.get().strip() or 0), 2)
        except ValueError:
            messagebox.showwarning('人员信息', '「发射秒数」要填数字，比如 0 或 1.5', parent=self)
            return
        self.result = d
        self.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
