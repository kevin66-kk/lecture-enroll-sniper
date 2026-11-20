# -*- coding: utf-8 -*-
"""
授权核心（随程序分发的部分）

思路：私钥只在签发方手上，程序里只嵌公钥。
授权文件 = 一段 JSON（被授权人 / 机器码 / 活动 eid / 到期时间）+ Ed25519 签名。
没有私钥就签不出能通过校验的文件，转发别人的授权也会因为机器码不符而被拒。

对外三个函数：
    machine_code()            本机机器码（给手下报给你的那串）
    check(eid=None)           校验结果 dict
    require(...)              校验不过就弹窗 + 退出
"""
import base64
import ctypes
import hashlib
import json
import os
import platform
import sys
import time
import uuid

LIC_NAME = "license.txt"
STATE_NAME = "auth_state.json"

# 由 boss_tool/boss_keygen.py 自动填入（公钥，公开无妨）
PUBKEY_B64 = "UoUfowil8_SdwatJLkIsNjZBh72HzSFo5xLOX7RpDqY="

# 本人机器：永久免授权（2026-11-06 用户明确要求）。
# 签发私钥随旧工具目录一起丢失，无法再续签，故把本机机器码写进白名单。
# 其他人的机器码不同，仍然要走正常授权流程。
#
# 2026-11-15 修正（当天自己也被挡在授权页）：
# 旧机器码 = sha256(C盘序列号 | uuid.getnode() | 架构)，而 uuid.getnode()
# 取的是第一块可用网卡的 MAC。换网卡、接手机热点、开 VPN 都会让 MAC 变，
# 机器码跟着漂，结果自己的电脑被当成「别的电脑」。现在主指纹改成 Windows
# 装机时生成的 MachineGuid（只有重装系统才会变），MAC 不再参与计算。
OWNER_GUIDS = set()
OWNER_MIDS = set()

# 开源副本说明：
#   上面两个集合原本存放作者本机的 MachineGuid / 机器码，用于「本人机器免授权」。
#   机器码属于本机硬件指纹，不适合随仓库公开，因此这里置空。
#   你需要的话，运行 `python auth_core.py` 会打印本机机器码，
#   把那一串填进 OWNER_MIDS，本机就永久免授权了。
PERMANENT_EXP = 4102444800          # 2100-01-01 00:00:00

# 开源副本默认「不启用授权」，克隆下来就能直接跑。
# 想自己用这套授权体系（签发 → 验签），把下面改成 True，
# 用你自己的 Ed25519 密钥对重新生成 PUBKEY_B64 并签发 license.txt。
# 私钥千万不要提交到仓库，否则任何人都能伪造授权。
REQUIRE_LICENSE = False

MSG_TITLE = "需要授权"


# ---------------------------------------------------------------- 路径
def base_dir():
    """打包成 exe 后取 exe 所在目录，否则取脚本目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def lic_path(name=LIC_NAME):
    return os.path.join(base_dir(), name)


# ---------------------------------------------------------------- 机器码
def _vol_serial(drive="C:\\"):
    try:
        k = ctypes.windll.kernel32
        serial = ctypes.c_uint32(0)
        ok = k.GetVolumeInformationW(ctypes.c_wchar_p(drive), None, 0,
                                     ctypes.byref(serial), None, None, None, 0)
        return serial.value if ok else 0
    except Exception:
        return 0


def _machine_guid():
    """Windows 装机时生成，只有重装系统才会变，比 MAC 稳得多"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Cryptography")
        return str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip().upper()
    except Exception:
        return ""


def machine_code():
    g = _machine_guid()
    if g:
        # 只用「C盘序列号 + MachineGuid + 架构」，MAC 完全不参与，
        # 插拔网卡 / 切热点 / 开 VPN 都不会再让机器码漂移。
        raw = "%s|%s|%s" % (_vol_serial(), g, platform.machine())
    else:
        raw = "%s|%s|%s" % (_vol_serial(), uuid.getnode(), platform.machine())
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()[:16]
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def _is_owner(mid=None):
    """本人机器判定：机器码命中，或 MachineGuid 命中（网卡怎么换都算）"""
    if str(mid or machine_code()).upper() in OWNER_MIDS:
        return True
    g = _machine_guid()
    return bool(g) and g in OWNER_GUIDS


# ---------------------------------------------------------------- 签名 / 校验
def _b64e(b):
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s):
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def canonical(payload):
    """固定序列化，避免 key 顺序不同导致签名对不上"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sign_payload(payload, private_key_b64):
    """私钥是 32 字节 raw Ed25519 seed 的 base64，只存在签发方机器上"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(_b64d(private_key_b64))
    sig = key.sign(canonical(payload))
    return "%s.%s" % (_b64e(canonical(payload)), _b64e(sig))


def verify_token(token):
    """返回 (payload, 错误原因)"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not PUBKEY_B64:
        return None, "程序里没有内置公钥（还没生成密钥）"
    try:
        p64, s64 = token.strip().split(".")
        payload = json.loads(_b64d(p64).decode("utf-8"))
        key = Ed25519PublicKey.from_public_bytes(_b64d(PUBKEY_B64))
        key.verify(_b64d(s64), canonical(payload))
        return payload, None
    except ValueError:
        return None, "授权文件格式不对"
    except Exception:
        return None, "授权文件签名无效（签发方对不上，或内容被改过）"


# ---------------------------------------------------------------- 时钟回拨
def _touch_clock(now):
    """记下见过的最晚时间，防止把系统时间调回去续命"""
    p = os.path.join(base_dir(), STATE_NAME)
    last = 0
    try:
        last = int(json.load(open(p, encoding="utf-8")).get("last_seen", 0))
    except Exception:
        pass
    if now + 86400 < last:
        return False, last
    try:
        json.dump({"last_seen": max(last, int(now))}, open(p, "w", encoding="utf-8"))
    except Exception:
        pass
    return True, last


# ---------------------------------------------------------------- 主校验
def check(eid=None):
    """eid 为当前要抢的活动号；授权绑定到具体 eid 时，换活动会失效"""
    now = time.time()
    mid = machine_code()
    out = {"ok": False, "reason": "", "mid": mid, "payload": None,
           "left_sec": 0, "path": lic_path()}

    # 本人机器永久放行：不走文件、不看到期，永远不会挡在授权页
    if _is_owner(mid):
        out["ok"] = True
        out["left_sec"] = PERMANENT_EXP - now
        out["payload"] = {"eid": "*", "exp": PERMANENT_EXP, "mid": mid,
                          "name": "本机", "note": "本机永久", "v": 1}
        return out

    # 开源副本：默认不启用授权校验（见文件顶部 REQUIRE_LICENSE）
    if not REQUIRE_LICENSE:
        out["ok"] = True
        out["left_sec"] = PERMANENT_EXP - now
        out["payload"] = {"eid": "*", "exp": PERMANENT_EXP, "mid": mid,
                          "name": "开源版", "note": "未启用授权", "v": 1}
        return out

    if not os.path.exists(out["path"]):
        out["reason"] = "还没有授权文件"
        return out
    try:
        token = open(out["path"], encoding="utf-8").read()
    except Exception as e:
        out["reason"] = "授权文件读不了：%s" % e
        return out

    payload, err = verify_token(token)
    if err:
        out["reason"] = err
        return out
    out["payload"] = payload

    if str(payload.get("mid", "")).upper() != mid:
        out["reason"] = "这份授权是别的电脑的（授权机器码 %s）" % payload.get("mid")
        return out

    exp = float(payload.get("exp") or 0)
    out["left_sec"] = exp - now
    if now >= exp:
        out["reason"] = "授权已过期（到期 %s）" % time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(exp))
        return out

    lic_eid = str(payload.get("eid") or "*")
    if eid is not None and lic_eid not in ("*", str(eid)):
        out["reason"] = "这份授权只对活动 %s 有效，当前活动是 %s" % (lic_eid, eid)
        return out

    ok_clock, last = _touch_clock(now)
    if not ok_clock:
        out["reason"] = ("检测到系统时间被调回（曾见过 %s），授权校验中止"
                         % time.strftime("%Y-%m-%d %H:%M", time.localtime(last)))
        return out

    out["ok"] = True
    return out


# ---------------------------------------------------------------- 失败时的提示
def _alert(text):
    """有窗口就弹窗，没窗口就打印"""
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:
        pass
    if os.environ.get("SNIPER_NO_DIALOG") == "1":
        return
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, MSG_TITLE, 0x10)
        except Exception:
            pass


def _apply_hint(app_name):
    return (
        "程序名：%s\n"
        "机器码：%s\n"
        "申请时间：%s\n"
    ) % (app_name, machine_code(),
         time.strftime("%Y-%m-%d %H:%M:%S"))


def require(app_name="讲座抢报助手", eid=None):
    """校验不通过就提示并退出（退出码 3）"""
    info = check(eid)
    if info["ok"]:
        return info

    hint = _apply_hint(app_name)
    req_file = os.path.join(base_dir(), "授权申请.txt")
    try:
        open(req_file, "w", encoding="utf-8").write(hint)
    except Exception:
        pass

    text = (
        "这个程序需要授权才能使用。\n\n"
        "原因：%s\n\n"
        "%s\n"
        "已把上面的信息存成「授权申请.txt」，\n"
        "把这个文件发给授权方，收到 license.txt 后\n"
        "放到本程序同一个文件夹里，再打开即可。"
    ) % (info["reason"], hint)
    _alert(text)
    sys.exit(3)


if __name__ == "__main__":
    i = check()
    print("机器码 :", i["mid"])
    print("授权   :", "有效" if i["ok"] else "无效 - " + i["reason"])
    if i["payload"]:
        p = i["payload"]
        print("被授权人:", p.get("name"))
        print("绑定活动:", p.get("eid"))
        print("到期    :", time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(float(p.get("exp") or 0))))
        print("剩余    : %.1f 小时" % (i["left_sec"] / 3600.0))
