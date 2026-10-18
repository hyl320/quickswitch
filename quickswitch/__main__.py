"""支持 ``python -m quickswitch`` 启动。"""

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
