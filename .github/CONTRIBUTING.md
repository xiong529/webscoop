# 贡献指南

欢迎贡献！项目为单人/小团队维护，一切以「低调、可用、不破坏现有行为」为准。

## 开发环境

- Python 3.11+（`python_requires` 以 3.11 为准）
- 依赖锁定：`pip install -r requirements.txt` 安装完整锁定版本；
  开发工具见 `requirements-dev.txt`（ruff / mypy / coverage）
- 不要手改 `.venv`；构建用 PyInstaller spec（`webscoop.spec` / `linux.spec`）

## 规矩

1. **先跑全量回归再提 PR**：`python tests/run_all.py unit` 全绿才提交；
   e2e 套件尽量也跑（本地 HTTP，除 `e2e_render` 需 chromium 可跳过）
2. **新增功能必须配测试**：往 `tests/unit_*.py` 里加 `check()` 断言即可，
   风格与现有套件一致；不要引入 pytest 等新框架
3. **类型**：核心模块（`resources_reptile/utils/proxy.py`、
   `resources_reptile/utils/cookies.py`、`secret_store.py`、`kvjournal.py`、
   `download_archive.py`、`dead_list.py`、`format_selector.py`）必须通过
   `mypy`（strict，见 `mypy.ini`）；新核心模块进白名单前先在仓库里声明
4. **风格**：`ruff check .` 必须零告警（配置见 `ruff.toml`）；
   docstring 中文，代码注释不写（命名自明）
5. **不引入重依赖**：评估一个库能否用标准库/现有依赖实现；
   新依赖必须同步锁进 `requirements.txt`/`requirements-dev.txt` 并说明理由
6. **兼容旧文件**：存档/死链表/规则表等持久化格式变更必须向后兼容读取
   （参考 `kvjournal.py` 的旧格式迁移），老用户升级不掉数据
7. **安全红线**：不得在代码/日志/发布物中留 API Key、Cookie、本机代理地址；
   敏感落盘走 `secret_store.py`；网络规则（正则/模板）做输入校验
8. **提交信息**：`type(scope): 中文摘要`，一次提交一个主题

## 发版流程（维护者）

发布 tag 由 CI 自动建 release（见 `.github/workflows/release.yml`），
Windows/Linux 双平台构建为本地流程（WSL 构建 linux.spec、Windows 构建
webscoop.spec），产物手动上传 tag 资产时用 `--clobber`。