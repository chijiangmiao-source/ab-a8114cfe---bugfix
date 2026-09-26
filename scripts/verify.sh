#!/usr/bin/env sh
# Compose verify 服务入口：
#   1) 代码测试（求解器：唯一故障 / 多解裁决 / 不可行；接口：提交身份语义）
#   2) 构建检查（全部源码字节码编译）
#   3) 重启持久化冒烟（同一 DB 上重启本地服务实例，真实 HTTP 验证取回与重试）
#   4) API 冒烟（对正在运行的 app 服务发起真实 HTTP 请求）
# 任一步失败即以非零退出码退出，全部通过以 0 退出。
set -eu

# 容器内为 python；部分本地环境只有 python3。
PY=$(command -v python || command -v python3)

echo "== [1/4] 单元与接口测试 =="
"$PY" tests/test_solver.py
"$PY" tests/test_api.py

echo "== [2/4] 构建检查（字节码编译）=="
"$PY" -m compileall -q app tests

echo "== [3/4] 重启持久化冒烟（本地实例，独立临时库）=="
"$PY" tests/smoke_restart.py

echo "== [4/4] API 冒烟（目标: ${APP_URL:-http://app:8080}）"
"$PY" tests/smoke_api.py

echo "== verify 全部通过 =="
