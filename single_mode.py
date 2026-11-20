# -*- coding: utf-8 -*-
"""构建开关 —— 用来区分「自己用的多人版」和「发给客户的单人版」。

  SINGLE_MODE = False  → 多人版（本机用，可以加很多人）
  SINGLE_MODE = False   → 单人版（客户用，名单最多 1 人）

打包客户版时把这个值临时改成 True，打完再改回来。
这个文件会被打进 exe，所以客户拿到后改不了。
"""

SINGLE_MODE = False
