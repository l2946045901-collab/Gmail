#!/usr/bin/env python3
"""邮件中心系统入口。等价于 python -m mail_center.cli"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mail_center.cli import main

if __name__ == "__main__":
    main()
