"""启动智能体 API（FastAPI）——本地演示 / 部署入口。

用法：
    python examples/api_server.py                       # http://127.0.0.1:8000/docs
    python examples/api_server.py --port 9000 --orchestrator graph

启动后典型的四步验证（脚本会把命令打印出来，可直接复制）：
    1) 签发令牌        POST /v1/auth/token
    2) 看权限与可见数据 GET  /v1/schema
    3) 问数            POST /v1/ask
    4) 审计            GET  /v1/audit   （需 admin）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 NL2SQL 智能体 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--orchestrator", default="pipeline", choices=["pipeline", "graph"])
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args(argv)

    import uvicorn

    from nl2sql.api import create_app
    from nl2sql.config import get_settings

    settings = get_settings()
    if settings.auth.enabled and not settings.auth.secret:
        print(
            "⚠️  未配置 AUTH__SECRET。本地演示请在 .env 里加上：\n"
            "      AUTH__SECRET=<任意随机串>\n"
            "      AUTH__DEV_TOKEN_ENDPOINT=true   # 允许用 /v1/auth/token 自助签发\n"
            "    （生产环境请务必使用固定的高强度随机密钥，并关闭 dev 端点）\n"
        )

    app = create_app(settings=settings)
    print(f"→ 接口文档: http://{args.host}:{args.port}/docs")
    print(f"→ 编排方式: {args.orchestrator}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
