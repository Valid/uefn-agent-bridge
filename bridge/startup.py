"""Auto-start the bridge when UEFN opens your project.

Place this file as:  <YourProject>/Content/Python/init_unreal.py

UEFN automatically runs any init_unreal.py found in its Python paths on
editor startup.  This stub imports the bridge and starts the HTTP server.

Prerequisites:
    Copy the bridge/ folder into the same Content/Python/ directory so that
    `import bridge.server` resolves correctly.

Docs: https://github.com/Valid/uefn-python-bridge
"""

import unreal


def _boot_bridge() -> None:
    try:
        from bridge import server

        if server._http is None:
            port = server.start()
            unreal.log(f"[Bridge] Auto-started on http://127.0.0.1:{port}")
        else:
            unreal.log(f"[Bridge] Already running on :{server._active_port}")
    except ImportError:
        unreal.log_warning(
            "[Bridge] bridge/server.py not found.  "
            "Copy the bridge/ folder to Content/Python/ - "
            "see https://github.com/Valid/uefn-python-bridge"
        )
    except Exception as exc:
        unreal.log_error(f"[Bridge] Auto-start failed: {exc}")


_boot_bridge()
