#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Behnam Farnaghinejad <behnam.farnaghinejad@polito.it>
# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import gc
import os
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gio, GLib, Gtk, WebKit2  # noqa: E402

STUDIO_ROOT = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ASE_STUDIO_HOST_ROOT", STUDIO_ROOT.parent)).resolve()
APPLICATION_ID = "it.polito.ASEStudio"
WM_CLASS = "ASEStudio"
ICON_PATH = STUDIO_ROOT / "frontend" / "icon.png"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ase_studio.backend as backend  # noqa: E402
from ase_studio.backend import (  # noqa: E402
    Handler, require_startup_repositories, terminate_active_commands,
)
from http.server import ThreadingHTTPServer  # noqa: E402


def create_server(preferred_port: int) -> ThreadingHTTPServer:
    try:
        return ThreadingHTTPServer(("127.0.0.1", preferred_port), Handler)
    except OSError:
        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def main() -> int:
    GLib.set_prgname(WM_CLASS)
    GLib.set_application_name("ASE Studio")
    if ICON_PATH.exists():
        Gtk.Window.set_default_icon_from_file(str(ICON_PATH))
    try:
        require_startup_repositories()
    except ValueError as error:
        message, _status = error.args[0]
        print(message, file=sys.stderr)
        dialog = Gtk.MessageDialog(
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Unsupported repository branches",
        )
        dialog.set_title("ASE Studio startup")
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
        return 1
    preferred_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

    application = Gtk.Application(application_id=APPLICATION_ID)
    server = None
    server_thread = None
    window = None
    view = None
    closing = False
    ui_destroyed = False

    def activate(app):
        nonlocal server, server_thread, window, view, closing, ui_destroyed
        if window is not None:
            window.present()
            return

        # Keep the application loop alive while WebKit is explicitly torn
        # down. Otherwise destroying the last window can end the loop before
        # WebKitGTK has released its page and signal handlers.
        app.hold()
        server = create_server(preferred_port)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        window = Gtk.ApplicationWindow(application=app, title="ASE Studio")
        # GLib.set_prgname supplies ASEStudio as the X11 WM_CLASS. The GTK
        # application ID supplies the corresponding Wayland application ID.
        window.set_default_size(1440, 900)
        window.set_size_request(900, 600)
        if ICON_PATH.exists():
            window.set_icon_from_file(str(ICON_PATH))

        view = WebKit2.WebView()
        view.get_settings().set_property("enable-developer-extras", False)

        def open_external_links(_view, decision, decision_type):
            if decision_type not in {
                    WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION}:
                return False
            navigation = decision.get_navigation_action()
            uri = navigation.get_request().get_uri()
            if (uri.startswith(("https://", "http://", "mailto:"))
                    and not uri.startswith("http://127.0.0.1:")):
                decision.ignore()
                Gio.AppInfo.launch_default_for_uri(uri, None)
                return True
            return False

        policy_handler = view.connect("decide-policy", open_external_links)

        def show_portal_sign_in(uid, url):
            # A private, ephemeral WebKit context keeps PoliTO cookies out of
            # the Studio view and discards them when the window closes.
            sign_in = Gtk.Window(title="PoliTO sign-in", transient_for=window,
                                 modal=True, destroy_with_parent=True)
            sign_in.set_default_size(560, 760)
            sign_in_view = WebKit2.WebView.new_with_context(
                WebKit2.WebContext.new_ephemeral())
            completed = False

            def finish(uri):
                nonlocal completed
                if completed:
                    return
                completed = True
                # Exchanging the SSO key is a network call; keep GTK responsive.
                threading.Thread(target=backend.portal_sso_callback,
                                 args=(uri,), daemon=True).start()
                sign_in.destroy()

            def intercept_callback(_view, decision, decision_type):
                if decision_type not in {
                        WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                        WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION}:
                    return False
                uri = decision.get_navigation_action().get_request().get_uri()
                if not uri.startswith(backend.PORTAL_SSO_CALLBACK):
                    return False
                decision.ignore()
                finish(uri)
                return True

            def intercept_failed_callback(_view, _event, failing_uri, _error):
                # Redirects to the unknown polito:// scheme may surface here.
                if not failing_uri.startswith(backend.PORTAL_SSO_CALLBACK):
                    return False
                finish(failing_uri)
                return True

            def sign_in_closed(_widget):
                if not completed:
                    backend.portal_sso_cancel(uid)

            sign_in_view.connect("decide-policy", intercept_callback)
            sign_in_view.connect("load-failed", intercept_failed_callback)
            sign_in.connect("destroy", sign_in_closed)
            sign_in.add(sign_in_view)
            sign_in.show_all()
            sign_in_view.load_uri(url)
            return GLib.SOURCE_REMOVE

        backend.PORTAL_SSO_OPENER = (
            lambda uid, url: GLib.idle_add(show_portal_sign_in, uid, url))

        def choose_download_destination(download, suggested_filename):
            dialog = Gtk.FileChooserDialog(
                title="Export pipeline",
                transient_for=window,
                action=Gtk.FileChooserAction.SAVE,
            )
            dialog.add_buttons(
                "Cancel", Gtk.ResponseType.CANCEL,
                "Save", Gtk.ResponseType.ACCEPT,
            )
            dialog.set_do_overwrite_confirmation(True)
            dialog.set_current_name(suggested_filename or "pipeline.csv")
            accepted = dialog.run() == Gtk.ResponseType.ACCEPT
            filename = dialog.get_filename() if accepted else None
            dialog.destroy()
            if filename:
                download.set_destination(Path(filename).resolve().as_uri())
            else:
                download.cancel()
            return True

        def handle_download(_context, download):
            download.connect("decide-destination", choose_download_destination)

        web_context = view.get_context()
        download_handler = web_context.connect("download-started", handle_download)
        view.load_uri(f"http://127.0.0.1:{server.server_address[1]}")
        window.add(view)

        def show_exit_confirmation(has_unsaved_changes: bool) -> bool:
            message = ("This project has unsaved changes. Exit ASE Studio and discard them?"
                       if has_unsaved_changes else "Exit ASE Studio?")
            dialog = Gtk.MessageDialog(
                transient_for=window,
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text=message,
            )
            dialog.set_title("Exit ASE Studio")
            response = dialog.run()
            dialog.destroy()
            return response == Gtk.ResponseType.YES

        def request_window_close(_window, _event) -> bool:
            nonlocal closing
            if closing:
                return True
            dirty = "• Unsaved" in (view.get_title() or "")
            if not show_exit_confirmation(dirty):
                return True
            closing = True
            view.stop_loading()
            # Stop the entire gem5/make process group before stopping HTTP.
            # Both operations run outside GTK's main loop so the window stays
            # responsive while children receive TERM and, if needed, KILL.
            def stop_backend():
                terminate_active_commands()
                server.shutdown()

            threading.Thread(target=stop_backend, daemon=True).start()
            return True

        window.connect("delete-event", request_window_close)
        window.show_all()

        def close_after_server_stops():
            nonlocal window, view, ui_destroyed
            if server_thread is not None and server_thread.is_alive():
                return GLib.SOURCE_CONTINUE
            if ui_destroyed:
                return GLib.SOURCE_REMOVE
            ui_destroyed = True
            if view is not None:
                view.disconnect(policy_handler)
                web_context.disconnect(download_handler)
                view.stop_loading()
                parent = view.get_parent()
                if parent is not None:
                    parent.remove(view)
                view.destroy()
                view = None
            if window is not None:
                doomed_window = window
                window = None
                doomed_window.destroy()

            # Give WebKitGTK one main-loop turn to release native resources
            # before the Gtk.Application and Python interpreter exit.
            def finish_application_shutdown():
                gc.collect()
                app.release()
                app.quit()
                return GLib.SOURCE_REMOVE

            GLib.timeout_add(100, finish_application_shutdown)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(250, close_after_server_stops)

    application.connect("activate", activate)
    try:
        return application.run([sys.argv[0]])
    finally:
        terminate_active_commands()
        if server_thread is not None and server_thread.is_alive():
            server.shutdown()
        if server is not None:
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
