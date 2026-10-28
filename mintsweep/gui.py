"""GTK interface for MintSweep.

Every action here is user initiated: the window opens with nothing selected,
nothing is deleted until the user ticks items and confirms, and no process is
asked to close without a confirmation naming it.
"""

from __future__ import annotations

import os
import threading
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import core  # noqa: E402

CSS = """
.card {
    background-color: alpha(currentColor, 0.06);
    border-radius: 10px;
    padding: 14px;
}
.big {
    font-size: 15pt;
    font-weight: bold;
}
.heading {
    font-weight: bold;
    font-size: 11pt;
}
.section-label {
    font-weight: bold;
    font-size: 9.5pt;
    opacity: 0.65;
    letter-spacing: 0.5px;
}
.row-size {
    font-feature-settings: "tnum";
    font-weight: bold;
}
.note {
    font-size: 8.5pt;
    opacity: 0.7;
}
.badge {
    background-color: alpha(#4caf7d, 0.22);
    border-radius: 6px;
    padding: 1px 7px;
    font-size: 8.5pt;
    font-weight: bold;
}
.badge-review {
    background-color: alpha(#e5a50a, 0.22);
    border-radius: 6px;
    padding: 1px 7px;
    font-size: 8.5pt;
    font-weight: bold;
}
diskbar block.filled {
    background-color: #4caf7d;
}
"""


def _dim(label: Gtk.Label) -> Gtk.Label:
    label.get_style_context().add_class("dim-label")
    return label


def _format_date(timestamp: float) -> str:
    if not timestamp:
        return "never"
    return time.strftime("%d %b %Y", time.localtime(timestamp))


class TargetRow(Gtk.ListBoxRow):
    """One selectable cleaning option."""

    def __init__(self, target: core.Target, on_toggle,
                 suggestion: "core.Suggestion | None" = None) -> None:
        super().__init__()
        self.target = target
        self.suggestion = suggestion
        self.set_activatable(False)
        self.set_selectable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_border_width(6)
        self.add(box)

        self.check = Gtk.CheckButton()
        self.check.set_active(False)  # nothing is ever preselected
        self.check.set_valign(Gtk.Align.CENTER)
        self.check.set_sensitive(target.has_content)
        self.check.connect("toggled", lambda _c: on_toggle())
        box.pack_start(self.check, False, False, 0)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label=target.label, xalign=0.0)
        title.get_style_context().add_class("heading")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title_box.pack_start(title, False, False, 0)
        if suggestion is not None:
            badge = Gtk.Label(label="Recommended" if suggestion.is_recommended else "Your call")
            badge.get_style_context().add_class(
                "badge" if suggestion.is_recommended else "badge-review")
            badge.set_tooltip_text(suggestion.reason)
            badge.set_valign(Gtk.Align.CENTER)
            title_box.pack_start(badge, False, False, 0)
        if target.requires_root:
            lock = _dim(Gtk.Label(label="needs admin", xalign=0.0))
            lock.get_style_context().add_class("note")
            title_box.pack_start(lock, False, False, 0)
        texts.pack_start(title_box, False, False, 0)

        desc = _dim(Gtk.Label(label=target.description, xalign=0.0, wrap=True))
        desc.set_max_width_chars(62)
        desc.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        texts.pack_start(desc, False, False, 0)

        if target.note:
            note = Gtk.Label(label=target.note, xalign=0.0)
            note.get_style_context().add_class("note")
            texts.pack_start(note, False, False, 0)

        clickable = Gtk.EventBox()
        clickable.set_visible_window(False)
        clickable.add(texts)
        clickable.connect("button-press-event", self._on_text_clicked)
        box.pack_start(clickable, True, True, 0)

        size_text = core.human_size(target.size) if target.has_content else "empty"
        self.size_label = Gtk.Label(label=size_text, xalign=1.0)
        self.size_label.get_style_context().add_class("row-size")
        if not target.has_content:
            self.size_label.get_style_context().add_class("dim-label")
        box.pack_end(self.size_label, False, False, 0)

        count_text = core.human_count(target.count) if target.has_content else ""
        count_label = _dim(Gtk.Label(label=count_text, xalign=1.0))
        count_label.get_style_context().add_class("note")
        box.pack_end(count_label, False, False, 6)

    def _on_text_clicked(self, _widget, _event) -> bool:
        if self.check.get_sensitive():
            self.check.set_active(not self.check.get_active())
        return True

    @property
    def selected(self) -> bool:
        return self.check.get_active()

    def clear(self) -> None:
        self.check.set_active(False)


class MintSweepWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="MintSweep")
        self.set_default_size(940, 720)
        self.set_size_request(720, 560)
        self.set_border_width(0)

        self._targets: list[core.Target] = []
        self._rows: list[TargetRow] = []
        self._target_suggestions: dict[str, core.Suggestion] = {}
        self._app_suggestions: list[core.Suggestion] = []

        self._files: list[core.FileInfo] = []
        self._scanning_files = False
        self._trashing_files = False
        self._games: list[core.Game] = []
        self._game_suggestions: list[core.Suggestion] = []
        self._games_loaded = False
        self._loading_games = False
        self._installed: list[core.InstalledApp] = []
        self._software_suggestions: list[core.Suggestion] = []
        self._apps_loaded = False
        self._loading_apps = False
        self._scanning = False
        self._cleaning = False
        self._groups: list[core.AppGroup] = []

        icon = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "assets", "mintsweep.svg")
        if os.path.exists(icon):
            try:
                self.set_icon_from_file(icon)
            except GLib.Error:
                pass

        self._build_header()
        self._build_body()

        self.connect("destroy", Gtk.main_quit)
        self.refresh_disk()
        self.update_selection()
        self.refresh_memory_numbers()
        self.refresh_processes()
        GLib.timeout_add_seconds(3, self._tick_memory)
        self.start_scan()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("MintSweep")
        header.set_subtitle("Storage & memory cleaner for Linux Mint")
        self.set_titlebar(header)

        self.scan_button = Gtk.Button(label="Look for files")
        self.scan_button.set_tooltip_text("Scan again for things you can safely remove")
        self.scan_button.connect("clicked", lambda _b: self.start_scan())
        header.pack_start(self.scan_button)

        menu_button = Gtk.MenuButton()
        menu_button.set_tooltip_text("About MintSweep")
        menu_button.set_image(Gtk.Image.new_from_icon_name("help-about-symbolic", Gtk.IconSize.BUTTON))
        about = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(14)
        title = Gtk.Label(label="MintSweep")
        title.get_style_context().add_class("heading")
        box.pack_start(title, False, False, 0)
        text = _dim(Gtk.Label(
            label="MintSweep only removes things you tick, and only after you confirm.\n"
                  "It never deletes anything outside your home folder on its own.",
            xalign=0.0, wrap=True))
        text.set_max_width_chars(42)
        box.pack_start(text, False, False, 0)
        box.show_all()
        about.add(box)
        menu_button.set_popover(about)
        header.pack_end(menu_button)

    def _build_body(self) -> None:
        self.notebook = Gtk.Notebook()
        self.notebook.set_border_width(0)
        self.notebook.append_page(self._build_storage_page(), Gtk.Label(label="  Storage  "))
        self.notebook.append_page(self._build_files_page(), Gtk.Label(label="  Files  "))
        self.notebook.append_page(self._build_games_page(), Gtk.Label(label="  Games  "))
        self.notebook.append_page(self._build_apps_page(), Gtk.Label(label="  Apps  "))
        self.notebook.append_page(self._build_memory_page(), Gtk.Label(label="  Memory  "))
        self.notebook.connect("switch-page", self._on_page_switch)
        self.add(self.notebook)

    # -- storage --------------------------------------------------------
    def _build_storage_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        top.set_border_width(14)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.get_style_context().add_class("card")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        disk_title = Gtk.Label(label="Disk space", xalign=0.0)
        disk_title.get_style_context().add_class("section-label")
        head.pack_start(disk_title, False, False, 0)
        self.disk_label = Gtk.Label(label="", xalign=1.0)
        head.pack_end(self.disk_label, False, False, 0)
        card.pack_start(head, False, False, 0)

        self.disk_bar = Gtk.LevelBar()
        self.disk_bar.set_min_value(0)
        self.disk_bar.set_max_value(1)
        self.disk_bar.get_style_context().add_class("diskbar")
        card.pack_start(self.disk_bar, False, False, 0)

        self.disk_detail = _dim(Gtk.Label(label="", xalign=0.0))
        card.pack_start(self.disk_detail, False, False, 0)

        top.pack_start(card, False, False, 0)

        hint = _dim(Gtk.Label(
            label="Tick only what you want to remove. Nothing is cleaned until you "
                  "press “Review & clean” and confirm.",
            xalign=0.0, wrap=True))
        top.pack_start(hint, False, False, 0)

        outer.pack_start(top, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.set_header_func(self._add_separators)
        scroll.add(self.list_box)
        outer.pack_start(scroll, True, True, 0)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.set_border_width(12)
        self.status_label = _dim(Gtk.Label(label="", xalign=0.0))
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        bar.pack_start(self.status_label, True, True, 0)

        self.spinner = Gtk.Spinner()
        bar.pack_start(self.spinner, False, False, 0)

        self.clean_button = Gtk.Button(label="Review & clean")
        self.clean_button.get_style_context().add_class("suggested-action")
        self.clean_button.set_sensitive(False)
        self.clean_button.connect("clicked", lambda _b: self.review_and_clean())
        bar.pack_end(self.clean_button, False, False, 0)

        self.selection_label = Gtk.Label(label="Nothing selected", xalign=1.0)
        self.selection_label.get_style_context().add_class("row-size")
        bar.pack_end(self.selection_label, False, False, 12)

        self.select_recommended_button = Gtk.Button(label="Select recommended")
        self.select_recommended_button.set_sensitive(False)
        self.select_recommended_button.set_tooltip_text(
            "Tick the items MintSweep recommends. You still review the list and "
            "confirm before anything is removed.")
        self.select_recommended_button.connect(
            "clicked", lambda _b: self.select_recommended_targets())
        bar.pack_end(self.select_recommended_button, False, False, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        outer.pack_start(bar, False, False, 0)
        return outer

    @staticmethod
    def _add_separators(row, before) -> None:
        if before is not None and row.get_header() is None:
            row.set_header(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

    # -- files ----------------------------------------------------------
    def _build_files_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(14)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="BIG FILES IN YOUR FOLDERS", xalign=0.0)
        title.get_style_context().add_class("section-label")
        head.pack_start(title, True, True, 0)

        self.file_size_combo = Gtk.ComboBoxText()
        for label, megabytes in (("bigger than 200 MB", 200),
                                 ("bigger than 500 MB", 500),
                                 ("bigger than 1 GB", 1000)):
            self.file_size_combo.append(str(megabytes), label)
        self.file_size_combo.set_active_id("200")
        head.pack_start(self.file_size_combo, False, False, 0)

        self.file_scan_button = Gtk.Button(label="Look for files")
        self.file_scan_button.connect("clicked", lambda _b: self.start_file_scan())
        head.pack_start(self.file_scan_button, False, False, 0)
        outer.pack_start(head, False, False, 0)

        hint = _dim(Gtk.Label(
            label="Your own files: nothing is deleted. Checked files move to Trash, "
                  "where you can put them back. Caches and project internals are skipped.",
            xalign=0.0, wrap=True))
        outer.pack_start(hint, False, False, 0)

        # checked, name, size, last used, path, note
        self.file_store = Gtk.ListStore(bool, str, str, str, str, str)
        view = Gtk.TreeView(model=self.file_store)
        view.get_selection().set_mode(Gtk.SelectionMode.NONE)
        view.connect("query-tooltip", lambda v, x, y, k, t:
                     self._tooltip_column(v, x, y, t, 4))

        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_file_toggled)
        view.append_column(Gtk.TreeViewColumn("", toggle, active=0))

        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_col = Gtk.TreeViewColumn("File", name_renderer, text=1)
        name_col.set_expand(True)
        view.append_column(name_col)

        size_renderer = Gtk.CellRendererText(xalign=1.0)
        size_col = Gtk.TreeViewColumn("Size", size_renderer, text=2)
        size_col.set_alignment(1.0)
        view.append_column(size_col)

        used_renderer = Gtk.CellRendererText()
        view.append_column(Gtk.TreeViewColumn("Last used", used_renderer, text=3))

        note_renderer = Gtk.CellRendererText()
        view.append_column(Gtk.TreeViewColumn("Note", note_renderer, text=5))
        self.file_view = view

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        outer.pack_start(scroll, True, True, 0)

        self.file_status = _dim(Gtk.Label(
            label="Press \u201cLook for files\u201d to scan Desktop, Documents, Downloads, "
                  "Videos, Pictures and Music.",
            xalign=0.0, wrap=True))
        outer.pack_start(self.file_status, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.file_select_button = Gtk.Button(label="Select unused (6+ months)")
        self.file_select_button.set_sensitive(False)
        self.file_select_button.set_tooltip_text(
            "Tick files that have not been opened or changed in over six months.")
        self.file_select_button.connect("clicked", lambda _b: self.select_unused_files())
        actions.pack_start(self.file_select_button, False, False, 0)

        self.file_trash_button = Gtk.Button(label="Move checked to Trash")
        self.file_trash_button.get_style_context().add_class("suggested-action")
        self.file_trash_button.set_sensitive(False)
        self.file_trash_button.connect("clicked", lambda _b: self.trash_checked_files())
        actions.pack_end(self.file_trash_button, False, False, 0)
        outer.pack_start(actions, False, False, 0)
        return outer

    def start_file_scan(self) -> None:
        if self._scanning_files:
            return
        self._scanning_files = True
        megabytes = int(self.file_size_combo.get_active_id() or 200)
        self.file_scan_button.set_sensitive(False)
        self.file_status.set_text("Looking for big files\u2026 this can take a moment.")
        threading.Thread(target=self._file_scan_worker,
                         args=(megabytes * 1024 * 1024,), daemon=True).start()

    def _file_scan_worker(self, min_bytes: int) -> None:
        def progress(root: str, folders: int) -> None:
            GLib.idle_add(self._set_file_status,
                          f"Scanning {root}\u2026 ({folders} folders checked)")
        try:
            files = core.find_large_files(min_bytes=min_bytes, progress=progress)
        except Exception as exc:  # pragma: no cover - defensive
            GLib.idle_add(self._file_scan_failed, str(exc))
            return
        GLib.idle_add(self._file_scan_done, files)

    def _set_file_status(self, text: str) -> bool:
        self.file_status.set_text(text)
        return False

    def _file_scan_failed(self, message: str) -> bool:
        self._scanning_files = False
        self.file_scan_button.set_sensitive(True)
        self.file_status.set_text(f"The scan stopped: {message}")
        return False

    def _file_scan_done(self, files: list[core.FileInfo]) -> bool:
        self._scanning_files = False
        self._files = files
        stale = {s.key[5:] for s in core.recommend_files(files)}
        self.file_store.clear()
        for item in files:
            self.file_store.append([
                False, item.name, core.human_size(item.size),
                _format_date(item.last_used), item.path,
                "unused 6+ months" if item.path in stale else ""])
        self.file_scan_button.set_sensitive(True)
        self.file_select_button.set_sensitive(bool(files))
        if files:
            total = sum(item.size for item in files)
            self.file_status.set_text(
                f"Found {len(files)} files, {core.human_size(total)} in total. "
                "Nothing is removed until you confirm.")
        else:
            self.file_status.set_text("No files that big were found in those folders.")
        self._update_file_actions()
        return False

    def _on_file_toggled(self, _renderer, path: str) -> None:
        self.file_store[path][0] = not self.file_store[path][0]
        self._update_file_actions()

    def _update_file_actions(self) -> None:
        checked = [row for row in self.file_store if row[0]]
        sizes = {item.path: item.size for item in self._files}
        total = sum(sizes.get(row[4], 0) for row in checked)
        self.file_trash_button.set_label(
            f"Move {len(checked)} to Trash ({core.human_size(total)})"
            if checked else "Move checked to Trash")
        self.file_trash_button.set_sensitive(bool(checked) and not self._trashing_files)

    def select_unused_files(self) -> None:
        stale = {s.key[5:] for s in core.recommend_files(self._files)}
        if not stale:
            self.file_status.set_text(
                "Nothing here is older than six months \u2014 no suggestions.")
            return
        for row in self.file_store:
            if row[4] in stale:
                row[0] = True
        self._update_file_actions()

    def trash_checked_files(self) -> None:
        chosen = [item for item, row in zip(self._files, self.file_store) if row[0]]
        if not chosen:
            return
        total = sum(item.size for item in chosen)

        dialog = Gtk.Dialog(title="Move to Trash", parent=self, flags=0)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Move to Trash", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(8)
        heading = Gtk.Label(
            label=f"Move {len(chosen)} file{'s' if len(chosen) != 1 else ''} "
                  f"({core.human_size(total)}) to Trash?",
            xalign=0.0, wrap=True)
        heading.get_style_context().add_class("big")
        content.pack_start(heading, False, False, 0)
        for item in chosen[:6]:
            line = _dim(Gtk.Label(label=f"\u2022 {item.name}  \u00b7  {core.human_size(item.size)}",
                                  xalign=0.0, wrap=True))
            line.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            content.pack_start(line, False, False, 0)
        if len(chosen) > 6:
            content.pack_start(_dim(Gtk.Label(label=f"\u2026and {len(chosen) - 6} more",
                                              xalign=0.0)), False, False, 0)
        content.pack_start(_dim(Gtk.Label(
            label="They stay in Trash, so you can restore them from there.",
            xalign=0.0, wrap=True)), False, False, 0)
        content.show_all()
        button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
        if button is not None:
            button.get_style_context().add_class("destructive-action")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        self._trashing_files = True
        self.file_trash_button.set_sensitive(False)
        self.file_status.set_text("Moving files to Trash\u2026")
        threading.Thread(target=self._trash_worker,
                         args=([item.path for item in chosen],), daemon=True).start()

    def _trash_worker(self, paths: list[str]) -> None:
        results = core.send_to_trash(paths)
        GLib.idle_add(self._trash_done, results)

    def _trash_done(self, results) -> bool:
        self._trashing_files = False
        moved = [path for path, ok, _msg in results if ok]
        failed = [(path, msg) for path, ok, msg in results if not ok]
        dialog = Gtk.MessageDialog(
            parent=self, flags=0,
            message_type=Gtk.MessageType.INFO if not failed else Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text=f"Moved {len(moved)} file{'s' if len(moved) != 1 else ''} to Trash")
        lines = [f"{os.path.basename(path)}: {message}" for path, message in failed]
        dialog.format_secondary_text(
            "\n".join(lines) if lines else "You can restore them from Trash.")
        dialog.run()
        dialog.destroy()
        self.start_file_scan()
        return False

    # -- games ----------------------------------------------------------
    def _build_games_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(14)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="STEAM GAMES", xalign=0.0)
        title.get_style_context().add_class("section-label")
        head.pack_start(title, True, True, 0)
        rescan = Gtk.Button(label="Rescan")
        rescan.connect("clicked", lambda _b: self.reload_games())
        head.pack_end(rescan, False, False, 0)
        outer.pack_start(head, False, False, 0)

        hint = _dim(Gtk.Label(
            label="Sizes, playtime and last-played dates come from your Steam library. "
                  "MintSweep never deletes game files: it asks Steam to uninstall, and "
                  "Steam shows its own confirmation. Cloud saves stay; local-only saves may not.",
            xalign=0.0, wrap=True))
        outer.pack_start(hint, False, False, 0)

        # name, size, playtime, last played, note, appid, install dir
        self.game_store = Gtk.ListStore(str, str, str, str, str, str, str)
        view = Gtk.TreeView(model=self.game_store)
        view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        view.get_selection().connect("changed", lambda _s: self._update_game_actions())
        view.connect("row-activated", lambda *_a: self.uninstall_selected_game())
        view.connect("query-tooltip", lambda v, x, y, k, t:
                     self._tooltip_column(v, x, y, t, 6))

        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_col = Gtk.TreeViewColumn("Game", name_renderer, text=0)
        name_col.set_expand(True)
        view.append_column(name_col)

        size_renderer = Gtk.CellRendererText(xalign=1.0)
        size_col = Gtk.TreeViewColumn("Size", size_renderer, text=1)
        size_col.set_alignment(1.0)
        view.append_column(size_col)
        view.append_column(Gtk.TreeViewColumn("Played", Gtk.CellRendererText(xalign=1.0), text=2))
        view.append_column(Gtk.TreeViewColumn("Last played", Gtk.CellRendererText(), text=3))
        view.append_column(Gtk.TreeViewColumn("Note", Gtk.CellRendererText(), text=4))
        self.game_view = view

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        outer.pack_start(scroll, True, True, 0)

        self.game_status = _dim(Gtk.Label(label="Reading your Steam library\u2026",
                                          xalign=0.0, wrap=True))
        outer.pack_start(self.game_status, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.game_select_button = Gtk.Button(label="Select suggested")
        self.game_select_button.set_sensitive(False)
        self.game_select_button.set_tooltip_text(
            "Select the unplayed game that takes the most space.")
        self.game_select_button.connect("clicked", lambda _b: self.select_suggested_game())
        actions.pack_start(self.game_select_button, False, False, 0)

        self.game_uninstall_button = Gtk.Button(label="Uninstall in Steam\u2026")
        self.game_uninstall_button.set_sensitive(False)
        self.game_uninstall_button.connect("clicked", lambda _b: self.uninstall_selected_game())
        actions.pack_end(self.game_uninstall_button, False, False, 0)
        outer.pack_start(actions, False, False, 0)
        return outer

    def reload_games(self) -> None:
        if self._loading_games:
            return
        self._loading_games = True
        self.game_status.set_text("Reading your Steam library\u2026")
        threading.Thread(target=self._games_worker, daemon=True).start()

    def _games_worker(self) -> None:
        try:
            games = core.steam_games()
            suggestions = core.recommend_games(games)
        except Exception:  # pragma: no cover - defensive
            GLib.idle_add(self._games_done, [], [])
            return
        GLib.idle_add(self._games_done, games, suggestions)

    def _games_done(self, games, suggestions) -> bool:
        self._loading_games = False
        self._games_loaded = True
        self._games = games
        self._game_suggestions = suggestions
        suggested = {s.key for s in suggestions}
        self.game_store.clear()
        for game in games:
            played = (f"{game.playtime_hours:.0f} h" if game.playtime_minutes
                      else "never")
            self.game_store.append([
                game.name, core.human_size(game.size), played,
                _format_date(game.last_played),
                "suggested" if f"game:{game.appid}" in suggested else "",
                game.appid, game.install_dir])
        if not games:
            self.game_status.set_text(
                "No Steam library found. If Steam lives somewhere unusual, MintSweep "
                "cannot see it.")
        elif suggestions:
            top = ", ".join(s.label for s in suggestions[:3])
            self.game_status.set_text(
                f"{len(games)} games installed. {len(suggestions)} look unused "
                f"({core.human_size(sum(s.size for s in suggestions))}): {top}")
        else:
            self.game_status.set_text(
                f"{len(games)} games installed, and all of them were played recently.")
        self.game_select_button.set_sensitive(bool(suggestions))
        self._update_game_actions()
        return False

    def _update_game_actions(self) -> None:
        model, tree_iter = self.game_view.get_selection().get_selected()
        self.game_uninstall_button.set_sensitive(tree_iter is not None)

    def select_suggested_game(self) -> None:
        if not self._game_suggestions:
            return
        target = self._game_suggestions[0].key
        for index, row in enumerate(self.game_store):
            if f"game:{row[5]}" == target:
                self.game_view.get_selection().select_path(Gtk.TreePath.new_from_indices([index]))
                self.game_view.scroll_to_cell(Gtk.TreePath.new_from_indices([index]),
                                              None, False, 0.0, 0.0)
                break
        self._update_game_actions()

    def uninstall_selected_game(self) -> None:
        model, tree_iter = self.game_view.get_selection().get_selected()
        if tree_iter is None:
            return
        appid, name, size = model[tree_iter][5], model[tree_iter][0], model[tree_iter][1]
        dialog = Gtk.MessageDialog(
            parent=self, flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Uninstall {name}?")
        dialog.format_secondary_text(
            f"{size} will be freed. Steam will open and ask you to confirm, so you can "
            "still cancel there. Save games stored only in the game folder may be removed.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        ok, message = core.open_steam_uninstall(appid)
        if not ok:
            error = Gtk.MessageDialog(
                parent=self, flags=0, message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK, text="Could not reach Steam")
            error.format_secondary_text(message)
            error.run()
            error.destroy()
        else:
            self.game_status.set_text(
                f"Steam is asking you to confirm removing {name}. "
                "Press Rescan when it is done.")

    # -- apps -----------------------------------------------------------
    def _build_apps_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(14)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="INSTALLED APPS", xalign=0.0)
        title.get_style_context().add_class("section-label")
        head.pack_start(title, True, True, 0)
        rescan = Gtk.Button(label="Rescan")
        rescan.connect("clicked", lambda _b: self.reload_apps())
        head.pack_end(rescan, False, False, 0)
        outer.pack_start(head, False, False, 0)

        hint = _dim(Gtk.Label(
            label="Flatpaks can be removed here. System packages are listed for review "
                  "only: dependencies matter, so MintSweep leaves those to Software Manager. "
                  "Last used is estimated from when the app's files were last accessed.",
            xalign=0.0, wrap=True))
        outer.pack_start(hint, False, False, 0)

        # name, size, last used, kind, note, key, detail
        self.app_store = Gtk.ListStore(str, str, str, str, str, str, str)
        view = Gtk.TreeView(model=self.app_store)
        view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        view.connect("query-tooltip", lambda v, x, y, k, t:
                     self._tooltip_column(v, x, y, t, 6))
        view.get_selection().connect("changed", lambda _s: self._update_app_actions())

        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_col = Gtk.TreeViewColumn("App", name_renderer, text=0)
        name_col.set_expand(True)
        view.append_column(name_col)
        size_renderer = Gtk.CellRendererText(xalign=1.0)
        size_col = Gtk.TreeViewColumn("Size", size_renderer, text=1)
        size_col.set_alignment(1.0)
        view.append_column(size_col)
        view.append_column(Gtk.TreeViewColumn("Last used", Gtk.CellRendererText(), text=2))
        view.append_column(Gtk.TreeViewColumn("Installer", Gtk.CellRendererText(), text=3))
        view.append_column(Gtk.TreeViewColumn("Note", Gtk.CellRendererText(), text=4))
        self.app_view = view

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        outer.pack_start(scroll, True, True, 0)

        self.app_status = _dim(Gtk.Label(label="Reading installed apps\u2026",
                                         xalign=0.0, wrap=True))
        outer.pack_start(self.app_status, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.pack_start(_dim(Gtk.Label(
            label="Select an app to remove a Flatpak.", xalign=0.0)), True, True, 0)
        self.app_remove_button = Gtk.Button(label="Uninstall Flatpak\u2026")
        self.app_remove_button.set_sensitive(False)
        self.app_remove_button.connect("clicked", lambda _b: self.uninstall_selected_app())
        actions.pack_end(self.app_remove_button, False, False, 0)
        outer.pack_start(actions, False, False, 0)
        return outer

    def reload_apps(self) -> None:
        if self._loading_apps:
            return
        self._loading_apps = True
        self.app_status.set_text("Reading installed apps\u2026")
        threading.Thread(target=self._apps_worker, daemon=True).start()

    def _apps_worker(self) -> None:
        try:
            apps = core.list_installed_apps()
            suggestions = core.recommend_unused_apps(apps)
        except Exception:  # pragma: no cover - defensive
            GLib.idle_add(self._apps_done, [], [])
            return
        GLib.idle_add(self._apps_done, apps, suggestions)

    def _apps_done(self, apps, suggestions) -> bool:
        self._loading_apps = False
        self._apps_loaded = True
        self._installed = apps
        self._software_suggestions = suggestions
        flagged = {s.key for s in suggestions}
        self.app_store.clear()
        for app in apps:
            used = _format_date(app.last_used) if app.last_used else "never"
            kind = "Flatpak" if app.kind == "flatpak" else "System"
            note = ("suggested" if f"app:{app.kind}:{app.key}" in flagged else "")
            self.app_store.append([app.name, core.human_size(app.size), used, kind,
                                   note, app.key, f"{kind} \u00b7 {app.key}"])
        if not apps:
            self.app_status.set_text("No applications found.")
        elif suggestions:
            self.app_status.set_text(
                f"{len(apps)} apps listed. {len(suggestions)} look unused "
                f"({core.human_size(sum(s.size for s in suggestions))}): "
                + ", ".join(s.label for s in suggestions[:3]))
        else:
            self.app_status.set_text(
                f"{len(apps)} apps listed. None look unused for more than six months.")
        self._update_app_actions()
        return False

    def _update_app_actions(self) -> None:
        model, tree_iter = self.app_view.get_selection().get_selected()
        if tree_iter is None:
            self.app_remove_button.set_sensitive(False)
            return
        self.app_remove_button.set_sensitive(model[tree_iter][3] == "Flatpak")

    def uninstall_selected_app(self) -> None:
        model, tree_iter = self.app_view.get_selection().get_selected()
        if tree_iter is None or model[tree_iter][3] != "Flatpak":
            return
        key, name, size = model[tree_iter][5], model[tree_iter][0], model[tree_iter][1]
        app = next((a for a in self._installed if a.kind == "flatpak" and a.key == key), None)
        if app is None:
            return

        dialog = Gtk.MessageDialog(
            parent=self, flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Uninstall {name}?")
        dialog.format_secondary_text(
            f"{size} will be freed. Your settings in ~/.var/app are kept, so reinstalling "
            "picks up where you left off. A password prompt may appear.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        self.app_status.set_text(f"Removing {name}\u2026")
        self.app_remove_button.set_sensitive(False)
        threading.Thread(target=self._uninstall_app_worker, args=(app,), daemon=True).start()

    def _uninstall_app_worker(self, app) -> None:
        ok, message = core.uninstall_flatpak(app)
        GLib.idle_add(self._uninstall_app_done, app.name, ok, message)

    def _uninstall_app_done(self, name: str, ok: bool, message: str) -> bool:
        dialog = Gtk.MessageDialog(
            parent=self, flags=0,
            message_type=Gtk.MessageType.INFO if ok else Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text=f"{name} removed" if ok else f"Could not remove {name}")
        dialog.format_secondary_text("" if ok else message)
        dialog.run()
        dialog.destroy()
        self.reload_apps()
        return False

    def _tooltip_column(self, view, x, y, tooltip, column: int) -> bool:
        found = view.get_path_at_pos(x, y)
        if not found:
            return False
        path, _column, _cell_x, _cell_y = found
        text = view.get_model()[path][column]
        if not text:
            return False
        tooltip.set_text(text)
        return True

    # -- memory ---------------------------------------------------------
    def _build_memory_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_border_width(14)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("card")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        head_title = Gtk.Label(label="Memory in use", xalign=0.0)
        head_title.get_style_context().add_class("section-label")
        head.pack_start(head_title, False, False, 0)
        self.mem_headline = Gtk.Label(label="", xalign=1.0)
        self.mem_headline.get_style_context().add_class("row-size")
        head.pack_end(self.mem_headline, False, False, 0)
        card.pack_start(head, False, False, 0)

        self.ram_bar = Gtk.LevelBar()
        self.ram_bar.set_min_value(0)
        self.ram_bar.set_max_value(1)
        card.pack_start(self.ram_bar, False, False, 0)

        self.mem_detail = _dim(Gtk.Label(label="", xalign=0.0, wrap=True))
        card.pack_start(self.mem_detail, False, False, 0)

        self.swap_label = _dim(Gtk.Label(label="", xalign=0.0))
        card.pack_start(self.swap_label, False, False, 0)
        self.swap_bar = Gtk.LevelBar()
        self.swap_bar.set_min_value(0)
        self.swap_bar.set_max_value(1)
        card.pack_start(self.swap_bar, False, False, 0)

        hint = Gtk.Label(
            label="Linux fills spare RAM with cached files and hands it back the moment "
                  "an app needs it. Cached memory is not wasted memory.",
            xalign=0.0, wrap=True)
        hint.get_style_context().add_class("note")
        card.pack_start(hint, False, False, 0)

        self.restart_label = Gtk.Label(label="", xalign=0.0, wrap=True)
        self.restart_label.get_style_context().add_class("note")
        card.pack_start(self.restart_label, False, False, 0)

        outer.pack_start(card, False, False, 0)

        procs_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label="Your biggest apps right now", xalign=0.0)
        label.get_style_context().add_class("section-label")
        procs_head.pack_start(label, True, True, 0)

        refresh = Gtk.Button(label="Refresh list")
        refresh.connect("clicked", lambda _b: self.refresh_processes())
        procs_head.pack_end(refresh, False, False, 0)
        outer.pack_start(procs_head, False, False, 0)

        self.mem_hint = _dim(Gtk.Label(label="", xalign=0.0, wrap=True))
        outer.pack_start(self.mem_hint, False, False, 0)

        # app, memory, note, instances, full reason (for the tooltip)
        self.proc_store = Gtk.ListStore(str, str, str, str, str)
        view = Gtk.TreeView(model=self.proc_store)
        view.set_headers_visible(True)
        view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        view.connect("row-activated", self._on_proc_activated)
        view.connect("query-tooltip", self._on_proc_tooltip)

        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        view.append_column(
            Gtk.TreeViewColumn("App", name_renderer, text=0))
        view.get_column(0).set_expand(True)
        ram_renderer = Gtk.CellRendererText(xalign=1.0)
        ram_col = Gtk.TreeViewColumn("Memory", ram_renderer, text=1)
        ram_col.set_alignment(1.0)
        view.append_column(ram_col)
        why_renderer = Gtk.CellRendererText()
        why_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        why_col = Gtk.TreeViewColumn("Note", why_renderer, text=2)
        why_col.set_min_width(70)
        view.append_column(why_col)
        pid_renderer = Gtk.CellRendererText(xalign=1.0)
        pid_col = Gtk.TreeViewColumn("Instances", pid_renderer, text=3)
        pid_col.set_alignment(1.0)
        view.append_column(pid_col)
        self.proc_view = view

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        outer.pack_start(scroll, True, True, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        note = Gtk.Label(
            label="Closing an app asks it to quit normally so it can save your work. "
                  "MintSweep never force-kills.",
            xalign=0.0, wrap=True)
        note.get_style_context().add_class("note")
        actions.pack_start(note, True, True, 0)

        self.close_button = Gtk.Button(label="Close selected app")
        self.close_button.set_sensitive(False)
        self.close_button.connect("clicked", lambda _b: self.close_selected_process())
        actions.pack_end(self.close_button, False, False, 0)

        drop = Gtk.Button(label="Release cache…")
        drop.set_tooltip_text("Ask the kernel to free cached file data (needs admin)")
        drop.connect("clicked", lambda _b: self.release_cache())
        actions.pack_end(drop, False, False, 0)

        self.select_apps_button = Gtk.Button(label="Select recommended")
        self.select_apps_button.set_sensitive(False)
        self.select_apps_button.set_tooltip_text(
            "Select the apps that look unused. Nothing closes until you press "
            "\u201cClose selected app\u201d and confirm.")
        self.select_apps_button.connect("clicked", lambda _b: self.select_recommended_apps())
        actions.pack_end(self.select_apps_button, False, False, 0)

        view.get_selection().connect("changed", lambda _s: self._update_close_button())
        outer.pack_start(actions, False, False, 0)
        return outer

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def start_scan(self) -> None:
        if self._scanning or self._cleaning:
            return
        self._scanning = True
        self.scan_button.set_sensitive(False)
        self.spinner.show()
        self.spinner.start()
        self.status_label.set_text("Looking for files you can safely remove…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        try:
            targets = core.builtin_targets()
            for target in targets:
                target.measure()
            caches = core.discover_cache_targets()
            self._targets = targets + caches
        except Exception as exc:  # pragma: no cover - defensive
            GLib.idle_add(self._scan_failed, str(exc))
            return
        GLib.idle_add(self._scan_done)

    def _scan_failed(self, message: str) -> bool:
        self._finish_scan()
        self.status_label.set_text(f"Could not finish scanning: {message}")
        return False

    def _scan_done(self) -> bool:
        self._fill_rows()
        self._finish_scan()
        total = core.total_size(self._targets)
        recommended = sum(1 for s in self._target_suggestions.values() if s.is_recommended)
        if total:
            suffix = f" {recommended} look safe to remove." if recommended else ""
            self.status_label.set_text(
                f"Found {core.human_size(total)} you could free.{suffix}")
        else:
            self.status_label.set_text("Nothing to clean right now — everything looks tidy.")
        return False

    def _finish_scan(self) -> None:
        self._scanning = False
        self.spinner.stop()
        self.spinner.hide()
        self.scan_button.set_sensitive(not self._cleaning)
        self.update_selection()

    def _fill_rows(self) -> None:
        for child in self.list_box.get_children():
            self.list_box.remove(child)
        self._rows.clear()
        self._target_suggestions = {s.key: s for s in core.recommend_targets(self._targets)}

        categories = list(dict.fromkeys(t.category for t in self._targets))
        for category in categories:
            header = Gtk.Label(label=category.upper(), xalign=0.0)
            header.get_style_context().add_class("section-label")
            header.set_margin_top(14)
            header.set_margin_bottom(4)
            header.set_margin_start(12)
            wrap = Gtk.ListBoxRow()
            wrap.set_selectable(False)
            wrap.set_activatable(False)
            wrap.add(header)
            self.list_box.add(wrap)

            for target in self._targets:
                if target.category != category:
                    continue
                row = TargetRow(target, self.update_selection,
                                self._target_suggestions.get(target.key))
                self.list_box.add(row)
                self._rows.append(row)

        self.list_box.show_all()
        self.update_selection()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def selected_targets(self) -> list[core.Target]:
        return [row.target for row in self._rows if row.selected and row.target.has_content]

    def update_selection(self) -> None:
        chosen = self.selected_targets()
        size = core.total_size(chosen)
        if not self._rows:
            self.selection_label.set_text("Nothing selected")
        elif chosen:
            self.selection_label.set_text(
                f"{len(chosen)} selected · {core.human_size(size)}")
        else:
            self.selection_label.set_text("Nothing selected")
        self.clean_button.set_sensitive(bool(chosen) and not self._cleaning and not self._scanning)

        suggested = [r for r in self._rows
                     if r.suggestion is not None and r.suggestion.is_recommended
                     and r.target.has_content]
        self.select_recommended_button.set_label(
            f"Select recommended ({len(suggested)})" if suggested else "Select recommended")
        self.select_recommended_button.set_sensitive(
            bool(suggested) and not self._cleaning and not self._scanning)

    def select_recommended_targets(self) -> None:
        """Tick the recommended rows. The user still reviews and confirms."""
        for row in self._rows:
            if (row.suggestion is not None and row.suggestion.is_recommended
                    and row.target.has_content):
                row.check.set_active(True)
        self.update_selection()

    def refresh_disk(self) -> None:
        info = core.disk_info("/")
        self.disk_bar.set_value(min(1.0, info.percent / 100.0))
        self.disk_label.set_text(f"{info.percent:.0f}% used")
        self.disk_detail.set_text(
            f"{core.human_size(info.used)} used of {core.human_size(info.total)}  ·  "
            f"{core.human_size(info.free)} free")

    # ------------------------------------------------------------------
    # Cleaning
    # ------------------------------------------------------------------
    def review_and_clean(self) -> None:
        chosen = self.selected_targets()
        if not chosen:
            return
        total = core.total_size(chosen)
        needs_root = any(t.requires_root for t in chosen)

        dialog = Gtk.Dialog(title="Review before cleaning", transient_for=self, flags=0)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Remove these", Gtk.ResponseType.OK)
        dialog.set_default_size(460, -1)
        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(10)

        heading = Gtk.Label(
            label=f"Remove {len(chosen)} item{'s' if len(chosen) != 1 else ''} and free "
                  f"{core.human_size(total)}?",
            xalign=0.0, wrap=True)
        heading.get_style_context().add_class("big")
        content.pack_start(heading, False, False, 0)

        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for target in chosen:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            dot = _dim(Gtk.Label(label="•", xalign=0.0))
            line.pack_start(dot, False, False, 0)
            name = Gtk.Label(label=target.label, xalign=0.0, wrap=True)
            line.pack_start(name, True, True, 0)
            amount = Gtk.Label(label=core.human_size(target.size), xalign=1.0)
            amount.get_style_context().add_class("row-size")
            line.pack_end(amount, False, False, 0)
            listing.pack_start(line, False, False, 0)
        content.pack_start(listing, False, False, 0)

        warnings = []
        if any(t.key == "trash" for t in chosen):
            warnings.append("Trash items will be gone for good.")
        if needs_root:
            warnings.append("You will be asked for your password for the system items.")
        if warnings:
            content.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
            for warning in warnings:
                label = Gtk.Label(label=warning, xalign=0.0, wrap=True)
                label.get_style_context().add_class("note")
                content.pack_start(label, False, False, 0)

        content.show_all()
        ok_button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
        if ok_button is not None:
            ok_button.get_style_context().add_class("destructive-action")

        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self._run_clean(chosen)

    def _run_clean(self, chosen: list[core.Target]) -> None:
        self._cleaning = True
        self.clean_button.set_sensitive(False)
        self.scan_button.set_sensitive(False)
        self.spinner.show()
        self.spinner.start()
        self.status_label.set_text("Cleaning the items you chose…")
        threading.Thread(target=self._clean_worker, args=(chosen,), daemon=True).start()

    def _clean_worker(self, chosen: list[core.Target]) -> None:
        try:
            results = core.clean_targets(chosen)
        except Exception as exc:  # pragma: no cover - defensive
            GLib.idle_add(self._clean_failed, str(exc))
            return
        GLib.idle_add(self._clean_done, results)

    def _clean_failed(self, message: str) -> bool:
        self._cleaning = False
        self.spinner.stop()
        self.spinner.hide()
        self.scan_button.set_sensitive(True)
        self.status_label.set_text(f"Cleaning stopped: {message}")
        return False

    def _clean_done(self, results) -> bool:
        self._cleaning = False
        self.spinner.stop()
        self.spinner.hide()
        self.scan_button.set_sensitive(True)

        freed = sum(r.freed for r in results if r.ok)
        failed = [r for r in results if not r.ok]

        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.INFO if not failed else Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text=f"Freed {core.human_size(freed)}")
        lines = [f"{r.target.label}: {core.human_size(r.freed)}" for r in results if r.ok]
        if failed:
            lines.append("")
            for result in failed:
                lines.append(f"{result.target.label}: {result.message}")
        dialog.format_secondary_text("\n".join(lines) or "Nothing needed removing.")
        dialog.run()
        dialog.destroy()

        self.refresh_disk()
        self.start_scan()
        return False

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    def _on_page_switch(self, _notebook, _page, index: int) -> None:
        if index == 2 and not self._games_loaded:
            self.reload_games()
        elif index == 3 and not self._apps_loaded:
            self.reload_apps()
        elif index == 4:
            self.refresh_memory_numbers()
            self.refresh_processes()

    def _tick_memory(self) -> bool:
        if self.notebook.get_current_page() == 1:
            self.refresh_memory_numbers()
        return True

    def refresh_memory_numbers(self) -> None:
        info = core.read_meminfo()
        self.ram_bar.set_value(min(1.0, info.used_percent / 100.0))
        self.mem_headline.set_text(
            f"{core.human_size(info.used)} of {core.human_size(info.total)} · {info.used_percent:.0f}%")
        self.mem_detail.set_text(
            f"{core.human_size(info.available)} available  ·  "
            f"{core.human_size(info.cached)} cached files  ·  "
            f"{core.human_size(info.free)} completely free")
        if info.swap_total:
            self.swap_label.set_text(
                f"Swap: {core.human_size(info.swap_used)} of {core.human_size(info.swap_total)} used")
            self.swap_bar.set_value(min(1.0, info.swap_percent / 100.0))
            self.swap_bar.show()
            self.swap_label.show()
        else:
            self.swap_label.set_text("Swap: not in use")
            self.swap_bar.hide()

        advice = core.restart_advice(info)
        self.restart_label.set_text(advice.summary)
        if advice.recommended:
            self.restart_label.set_tooltip_text(
                "\n".join(f"\u2022 {reason}" for reason in advice.reasons)
                + "\n\nSave your work, then restart from the Mint menu.")
        else:
            self.restart_label.set_tooltip_text("")
        self.restart_label.set_opacity(1.0 if advice.recommended else 0.7)

    def refresh_processes(self) -> None:
        # Read more than we show so grouping by app is accurate. Skip ourselves
        # so nobody can accidentally close MintSweep mid-clean.
        me = os.getpid()
        procs = [p for p in core.top_processes(limit=400)
                 if p.owned_by_me and p.pid != me]
        groups = core.group_processes(procs)
        self._app_suggestions = core.recommend_apps(groups)
        suggested = {s.label: s for s in self._app_suggestions}
        self._groups = groups[:25]

        self.proc_store.clear()
        for group in self._groups:
            suggestion = suggested.get(group.name)
            self.proc_store.append([
                group.name,
                core.human_size(group.rss),
                "idle" if suggestion else "",
                group.instance_label,
                suggestion.reason if suggestion else "",
            ])
        self.close_button.set_sensitive(False)

        if self._app_suggestions:
            count = len(self._app_suggestions)
            total = sum(s.size for s in self._app_suggestions)
            names = ", ".join(s.label for s in self._app_suggestions[:3])
            if count > 3:
                names += f" and {count - 3} more"
            plural = count == 1
            self.mem_hint.set_text(
                f"{count} {'app looks' if plural else 'apps look'} unused and "
                f"{'holds' if plural else 'hold'} {core.human_size(total)} ({names}). "
                "Hover a row for why; closing it is still your choice.")
        else:
            self.mem_hint.set_text(
                "Nothing to suggest: every app using a lot of memory is busy right now.")
        self.select_apps_button.set_sensitive(bool(self._app_suggestions))

    def select_recommended_apps(self) -> None:
        """Highlight the idle apps in the list. Nothing is closed here."""
        names = {s.label for s in self._app_suggestions}
        selection = self.proc_view.get_selection()
        selection.unselect_all()
        for index, group in enumerate(self._groups):
            if group.name in names:
                selection.select_path(Gtk.TreePath.new_from_indices([index]))
        self._update_close_button()

    def _on_proc_tooltip(self, view, x, y, _keyboard_mode, tooltip) -> bool:
        found = view.get_path_at_pos(x, y)
        if not found:
            return False
        path, _column, _cell_x, _cell_y = found
        reason = view.get_model()[path][4]
        if not reason:
            return False
        tooltip.set_text(reason)
        return True

    def _update_close_button(self) -> None:
        model, rows = self.proc_view.get_selection().get_selected_rows()
        self.close_button.set_sensitive(bool(rows))

    def _selected_groups(self) -> list[core.AppGroup]:
        model, paths = self.proc_view.get_selection().get_selected_rows()
        names = {model[path][0] for path in paths}
        return [g for g in self._groups if g.name in names]

    def _on_proc_activated(self, _view, _path, _column) -> None:
        self.close_selected_process()

    def close_selected_process(self) -> None:
        chosen = self._selected_groups()
        if not chosen:
            return
        names = ", ".join(f"{g.name} ({core.human_size(g.rss)})" for g in chosen[:3])
        if len(chosen) > 3:
            names += f" and {len(chosen) - 3} more"
        total = sum(len(g.pids) for g in chosen)
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Close {names}?")
        dialog.format_secondary_text(
            f"This asks {total} process{'es' if total != 1 else ''} to quit normally so they "
            "can save your work. Unsaved changes may still be lost. "
            "Only apps you started are listed here.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        failed = []
        pids = [pid for group in chosen for pid in group.pids]
        for pid, ok, message in core.end_processes(pids):
            if not ok:
                failed.append(f"{pid}: {message}")
        if failed:
            error = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK,
                text="Some apps could not be closed")
            error.format_secondary_text("\n".join(failed))
            error.run()
            error.destroy()

        GLib.timeout_add(600, self._refresh_processes_later)

    def _refresh_processes_later(self) -> bool:
        self.refresh_memory_numbers()
        self.refresh_processes()
        return False

    def release_cache(self) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Release cached file data?")
        dialog.format_secondary_text(
            "Linux uses spare RAM to cache files, and frees that memory automatically "
            "when an app needs it.\n\n"
            "Releasing it now makes the free-memory number look better, but the next few "
            "minutes may feel slower while the cache refills. No apps or files are touched.\n\n"
            "This asks for your administrator password.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        self.status_label.set_text("Asking the kernel to release cached memory…")
        threading.Thread(target=self._release_worker, daemon=True).start()

    def _release_worker(self) -> None:
        ok, message = core.drop_caches()
        GLib.idle_add(self._release_done, ok, message)

    def _release_done(self, ok: bool, message: str) -> bool:
        if not ok:
            dialog = Gtk.MessageDialog(
                transient_for=self, flags=0,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK,
                text="Cache was not released")
            dialog.format_secondary_text(message)
            dialog.run()
            dialog.destroy()
        self.refresh_memory_numbers()
        return False


def _load_css() -> None:
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(CSS.encode("utf-8"))
    except GLib.Error:
        return
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def main() -> int:
    if not Gtk.init_check(None)[0]:
        print("MintSweep needs a graphical display. Use the command line instead: "
              "mintsweep scan")
        return 1
    _load_css()
    window = MintSweepWindow()
    window.show_all()
    window.spinner.hide()
    window.update_selection()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
