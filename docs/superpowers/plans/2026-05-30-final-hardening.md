# Final Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the remaining low-to-medium risk hardening work in one `0.1.9` release.

**Architecture:** Keep the existing Flask/Jinja plus shell-script architecture. Add contract tests for the remaining risky surfaces, then make minimal compatible changes: local static vendor assets, shell-safe env writes, safer crontab updates, and conservative systemd hardening.

**Tech Stack:** Python unittest, Flask/Jinja, Bash scripts, systemd unit files, vendored Bootstrap assets.

---

### Task 1: Final Contract Tests

**Files:**
- Create: `tests/test_final_hardening_contract.py`

- [x] Write failing tests that require local frontend assets, safe installer `.env` writing, safe legacy env updates, safer uninstall crontab cleanup, conservative systemd hardening, and `PANEL_VERSION = "0.1.9"`.
- [x] Run `python -m unittest tests.test_final_hardening_contract -v` and confirm the tests fail for the current code.

### Task 2: Implement Final Hardening

**Files:**
- Modify: `install.sh`
- Modify: `remote-root/usr/bin/mihomo`
- Modify: `remote-root/etc/mihomo/scripts/manage_config.sh`
- Modify: `remote-root/etc/mihomo/scripts/set_notify.sh`
- Modify: `remote-root/etc/mihomo/scripts/uninstall.sh`
- Modify: `remote-root/etc/mihomo/scripts/service_ctl.sh`
- Modify: `remote-root/etc/systemd/system/*.service`
- Modify: `remote-root/etc/mihomo/manager/templates/index.html`
- Add: `remote-root/etc/mihomo/manager/static/vendor/*`

- [x] Replace installer raw env heredoc values with `write_env_line` using `printf %q`.
- [x] Replace legacy `sed -i` env writes with Python `shlex.quote` upsert helpers.
- [x] Replace uninstall crontab pipe cleanup with a temp-file workflow and fixed-string filtering.
- [x] Add conservative service hardening: `RestartSec`, `NoNewPrivileges`, `PrivateTmp`, and `UMask`.
- [x] Vendor Bootstrap CSS, Bootstrap Icons CSS/fonts, and Bootstrap JS under `/static/vendor`.
- [x] Bump panel version to `0.1.9`.

### Task 3: Verification and Release

**Files:**
- Modify tests as needed only if expectations are mechanically tied to the version string.

- [x] Run `python -m unittest discover -s tests -v`.
- [x] Run `python -m py_compile remote-root\etc\mihomo\manager\app.py`.
- [x] Run `git diff --check`.
- [ ] Commit and push to `origin/main`.
