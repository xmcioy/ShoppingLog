#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
以守护进程方式启动一个命令（供 deploy.sh / run.sh 使用）。

为什么不用 `nohup cmd &`
------------------------
`nohup` 只忽略 SIGHUP。如果父 shell 的**进程组**被整体回收（终端关闭、
CI/自动化会话结束、任务清理），子进程照样会被 SIGTERM 带走。
部署脚本给出的 `DEPLOY_OK` 就成了"检查那一刻是活的，两秒后死了"。

这里的做法是经典 double-fork + `setsid()`：
  1. fork 出子进程，父进程立刻退出（返回 pid 给调用方）
  2. 子进程 `setsid()` 脱离控制终端、成为新会话/新进程组的组长
  3. 再 fork 一次，中间进程退出，孙进程被 init 收养
  4. 孙进程把 0/1/2 重定向到日志与 /dev/null，再 `execv` 成目标程序

结果：服务进程不在任何终端的进程组里，终端关闭 / 任务清理都影响不到它。

用法：
  SHOPLOG_LOG=/path/to/server.log python3 scripts/start_daemon.py server.py --port 8787
  # 成功时把孙进程 pid 打到 stdout，失败返回非 0
"""

import os
import sys


def main():
    argv = sys.argv[1:]
    if not argv:
        print("用法: start_daemon.py <程序> [参数...]", file=sys.stderr)
        return 2

    log_path = os.environ.get("SHOPLOG_LOG") or os.devnull

    # ---- 第一次 fork ----
    pid = os.fork()
    if pid > 0:
        # 父进程：把子进程 pid 交给调用方后退出
        print(pid)
        sys.stdout.flush()
        return 0

    # ---- 子进程：脱离终端 ----
    os.setsid()

    # ---- 第二次 fork：确保不是会话首进程，无法再获得控制终端 ----
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # ---- 孙进程：这才是真正的服务 ----
    try:
        # 切到根目录，避免占住调用方的 cwd 导致该目录无法卸载
        os.chdir(os.environ.get("SHOPLOG_CWD") or os.getcwd())
    except OSError:
        pass

    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.close(null_fd)

    try:
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        log_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    if log_fd > 2:
        os.close(log_fd)

    try:
        os.execv(sys.executable, [sys.executable, *argv])
    except OSError as e:
        sys.stderr.write(f"execv 失败: {e}\n")
        os._exit(127)


if __name__ == "__main__":
    sys.exit(main())
