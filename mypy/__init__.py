# ex:ts=4:et:

import enum
import functools
import gi
import re
import subprocess
import sys
import warnings
from typing import Optional

from .gutterrenderer import GutterRenderer

gi.require_version('Gedit', '3.0')
gi.require_version('Gtk', '3.0')

from gi.repository import GObject, Gedit, GLib, GtkSource, Gtk, Pango, PeasGtk, Gio  # noqa

PROJECT_FILES = (".mypy.ini", "pyproject.toml", "setup.cfg")


@enum.unique
@functools.total_ordering
class Level(enum.Enum):
    NOTE = ("note", "#007FFF")
    WARN = ("warning", "#f5c200")
    ERROR = ("error", "#c01c28")
    UNKNOWN = ("?", "#c64600")
    
    def __lt__(self, other):
        members = list(type(self).__members__.values())
        a = members.index(self)
        b = members.index(other)
        return a < b
    
    @classmethod
    def by_code(clz, code):
        for level in Level.__members__.values():
            if level.code == code:
                return level
        return clz.UNKNOWN
    
    @property
    def code(self):
        return self.value[0]
    
    @property
    def color(self):
        return self.value[1]


class Message:
    path: str
    line: int
    column: int
    end_line: int
    end_column: int
    level: Level
    message: str
    rule: Optional[str]
    
    def __init__(self, view, match):
        self.view = view
        
        d = match.groupdict()
        
        self.path = d["path"]
        self.line = int(d["line"])
        self.column = int(d["column"])
        self.end_line = int(d["end_line"])
        self.end_column = int(d["end_column"])
        self.level_text = d["level"]
        self.level = Level.by_code(self.level_text)
        self.message = d["message"]
        self.rule = d["rule"]
        
        iter_start = self.buffer.get_iter_at_line_offset(self.line - 1, self.column - 1)
        iter_end = self.buffer.get_iter_at_line_offset(self.end_line - 1, self.end_column - 1)
        self.mark_start = self.buffer.create_mark(None, iter_start, False)
        self.mark_end = self.buffer.create_mark(None, iter_end, True)
        self.mark_start.set_visible(False)
        self.mark_end.set_visible(False)
    
    @property
    def buffer(self):
        return self.view.buffer
    
    def get_file(self):
        return Gio.File.new_for_path(self.path)
    
    def __repr__(self):
        return "Message({})".format(", ".join(
            f"{attr}={getattr(self, attr)!r}"
            for attr
            in self.__annotations__.keys()
            if attr not in ["path"]
        ))
    
    def get_pango_markup(self):
        text = GLib.markup_escape_text(self.message)
        
        return (
            f'{self.line}<span foreground="#008899">:</span>'
            f'{self.column}<span foreground="#008899">:</span> '
            f'<span foreground="{self.level.color}"><b>{self.level_text}:</b></span> {text}'
            + (f' <span foreground="#916a42">[{self.rule}]</span>' if self.rule else "")
        )


class MyPyViewActivatable(GObject.Object, Gedit.ViewActivatable):
    view = GObject.Property(type=Gedit.View)
    
    def __init__(self):
        super().__init__()
        
        self.context_data = []
        self.parse_signal = 0
        self.connected = False
        self.location = None
    
    def do_activate(self):
        self.gutter_renderer = GutterRenderer(self)
        self.gutter = self.view.get_gutter(Gtk.TextWindowType.LEFT)
        
        self.view_signals = [
            self.view.connect('notify::buffer', self.on_notify_buffer),
        ]
        
        self.buffer = None
        self.on_notify_buffer(self.view)
    
    def do_deactivate(self):
        if self.parse_signal != 0:
            GLib.source_remove(self.parse_signal)
            self.parse_signal = 0
        
        self.disconnect_buffer()
        self.buffer = None
        
        self.disconnect_view()
        self.gutter.remove(self.gutter_renderer)
    
    def disconnect(self, obj, signals):
        for sid in signals:
            obj.disconnect(sid)
        
        signals[:] = []
    
    def disconnect_buffer(self):
        self.disconnect(self.buffer, self.buffer_signals)
    
    def disconnect_view(self):
        self.disconnect(self.view, self.view_signals)
    
    def on_notify_buffer(self, view, gspec=None):
        if self.parse_signal != 0:
            GLib.source_remove(self.parse_signal)
            self.parse_signal = 0
        
        if self.buffer:
            self.disconnect_buffer()
        
        self.buffer = view.get_buffer()
        
        # The changed signal is connected to in update_location().
        self.buffer_signals = [
            self.buffer.connect('saved', self.update_location),
            self.buffer.connect('loaded', self.update_location),
            self.buffer.connect('notify::language', self.update_location),
        ]
    
    def should_check(self):
        if self.location is None:
            return False
        
        if self.buffer.get_language().get_id().startswith("python"):
            return True
        
        return False
    
    def update_location(self, *unused):
        old_location = self.location
        self.location = self.buffer.get_file().get_location()
        
        if not self.should_check():
            self.disconnect_gutter()
            return
        
        try:
            if (not old_location) or not self.location.equal(old_location):
                self.project_folder = self.find_project_folder()
        except FileNotFoundError:
            self.disconnect_gutter()
        else:
            self.connect_gutter()
            self.update()
    
    def find_project_folder(self):
        if not self.location.has_parent():
            raise FileNotFoundError("File has no parent")
        
        folder = self.location
        while folder.has_parent():
            folder = folder.get_parent()
            
            for filename in PROJECT_FILES:
                if folder.get_child(filename).query_exists():
                    return folder
        
        return self.location.get_parent()
    
    def disconnect_gutter(self):
        if not self.connected:
            return
        
        self.gutter.remove(self.gutter_renderer)
        self.buffer.disconnect(self.buffer_signals.pop())
        self.connected = False
    
    def connect_gutter(self):
        if self.connected:
            self.update()
            return
        
        self.gutter.insert(self.gutter_renderer, 50)
        self.buffer_signals.append(self.buffer.connect('saved', self.update))
        self.buffer_signals.append(self.buffer.connect('changed', self.update_gutter))
        self.connected = True
    
    def update(self, *unused):
        if self.parse_signal != 0:
            GLib.source_remove(self.parse_signal)
            self.parse_signal = 0
        
        if not self.buffer:
            self.context_data = []
        
        try:
            proc = subprocess.Popen(
                (
                    "mypy",
                    "--no-error-summary",
                    "--show-absolute-path",
                    "--show-column-numbers",
                    "--show-error-end",
                    self.location.get_path()
                ),
                cwd=self.project_folder.get_path(),
                stdout=subprocess.PIPE,
                universal_newlines=True,
            )
        except FileNotFoundError as e:
            warnings.warn("mypy could not be found in $PATH: " + str(e))
            return
        
        data = ""
        
        def on_read(stdout, flags, proc):
            nonlocal data
            
            data += stdout.read(4096)
            if not (flags & GLib.IO_HUP):
                return True
            
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                return True
            
            data += stdout.read()
            self.parse_mypy(data)
            self.parse_signal = 0
            return False
        
        self.parse_signal = GLib.io_add_watch(proc.stdout, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_read, proc)
    
    def parse_mypy(self, data):
        if not data:
            lines = []
        else:
            lines = data.strip("\n").split("\n")
        
        context_data = []
        
        for line in lines:
            match = re.match(
                r"""
                    ^
                    (?P<path>.+):
                    (?P<line>\d+):
                    (?P<column>\d+):
                    (?P<end_line>\d+):
                    (?P<end_column>\d+):
                    \s+(?P<level>[a-z]+):
                    \s+(?P<message>.*?)
                    (?:\s+\[(?P<rule>[a-z]+)\])?
                    $
                """,
                line,
                flags=re.I | re.VERBOSE,
            )
            if not match:
                print("Unknown line:", repr(line), file=sys.stderr)
                continue
            
            msg = Message(self, match)
            if not self.location.equal(msg.get_file()):
                continue
            context_data.append(msg)
        
        self.context_data = context_data
        
        self.update_gutter()
    
    def update_gutter(self, *unused):
        self.gutter_renderer.update()

