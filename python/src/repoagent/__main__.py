"""模块入口：执行 python -m repoagent 时，转交命令行处理函数。"""

from repoagent.cli import main

# 将返回值作为进程退出码，方便终端和自动化脚本判断运行结果。
raise SystemExit(main())
