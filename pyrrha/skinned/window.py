# -*- coding: utf-8 -*-
# Pyrrha - a Qt port of Pithos.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3.

"""The classic Winamp skinned main window (prototype).

Renders a fixed 275x116 frameless window entirely from a :class:`Skin`'s
sprites and drives a controller (a ``PyrrhaWindow``). Coordinates follow the
classic Winamp 2 skin spec; a few (title/time placement) are easy to tune
against a real skin.
"""

import logging
import math
import os
import time

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QRegion
from PySide6.QtWidgets import QFileDialog, QMenu, QWidget

from .eqwindow import SkinnedEqWindow
from .font import CHAR_H, CHAR_W, NUM_H, NUM_W, NumberFont, TextFont
from .playlistwindow import SkinnedPlaylistWindow

W, H = 275, 116  # classic main-window native size
WMAX = 1600      # max logical content width the shell can grow to
SNAP_DIST = 12   # logical px within which a dragged panel snaps to another edge
CANVAS_MAX = 4096  # fallback drag bound (device px) when no screen is available
# Classic mode is a screen-sized, transparent, click-through overlay: the shell
# window covers the whole screen, panels are positioned freely inside it (main at
# its stored spot, EQ/playlist relative to main), and the window mask exposes only
# the panels so the rest of the screen clicks through. This removes the old
# reserved-room size ceiling — panels can be arranged anywhere on the screen.

# A Wayland client cannot position its own top-level window (Qt move() is a
# no-op), and KWin re-anchors the top-left on every client-driven resize. So
# after a mode switch resizes the shell, we ask KWin to place the window
# explicitly via a one-shot scripting call (verified ~1ms on KWin). The script
# matches our window by PID + caption 'Pyrrha': while the skinned shell is up the
# standard window is hidden (unmapped, so absent from KWin's list) and the EQ/
# playlist are child widgets, so the shell is the sole mapped 'Pyrrha' top-level.
# KDE-only; on anything else the D-Bus call simply fails and is ignored.
_KWIN_MOVE_JS = """(function () {
    var PID = %(pid)d, X = %(x)d, Y = %(y)d, W = %(w)d, H = %(h)d;
    var list = (typeof workspace.windowList === "function")
        ? workspace.windowList()
        : (typeof workspace.clientList === "function" ? workspace.clientList() : []);
    function place(c) {
        var r = { x: X, y: Y, width: W, height: H };
        try { c.frameGeometry = r; return true; } catch (e) {}
        try { c.geometry = r; return true; } catch (e) {}
        return false;
    }
    var fallback = null;
    for (var i = 0; i < list.length; i++) {
        var c = list[i];
        try {
            if (c.pid !== PID) continue;
            if (c.caption === "Pyrrha") { place(c); return; }
            if (fallback === null) fallback = c;
        } catch (e) {}
    }
    if (fallback !== null) place(fallback);
})();
"""

# The main window stretches by keeping a fixed left slice and a fixed
# right-anchored slice, filling the inserted gap with the skin's own pixels.
SPLIT = 219      # width of the left slice kept as-is (right slice = W-SPLIT)
TB_CORNER = 25   # title-bar end-cap width (carries the menu/close/min glyphs)
TB_TITLE = 100   # centered title graphic width, re-centered as the bar widens

# name, dest x/y/w/h, source x, source y (normal), source y (pressed)
BUTTONS = [
    ('prev', 16, 88, 23, 18, 0, 0, 18),
    ('play', 39, 88, 23, 18, 23, 0, 18),
    ('pause', 62, 88, 23, 18, 46, 0, 18),
    ('stop', 85, 88, 23, 18, 69, 0, 18),
    ('next', 108, 88, 22, 18, 92, 0, 18),
    ('eject', 136, 89, 22, 16, 114, 0, 16),
]

TITLEBAR = (0, 0, 275, 14)          # dest; src active (27,0), inactive (27,15)
# Windowshade titlebar: src active (27,29), inactive (27,44). It bakes the title
# label, a black display (seek + a ':' time slot at x144) and the mini transport;
# we scroll the song title in the display and draw the elapsed time on the ':'.
WS_TB_ACTIVE, WS_TB_INACTIVE = 29, 44
WS_TITLE = (79, 4, 52)              # scrolling-title area in the shade display
WS_COLON_X, WS_TIME_Y = 144, 4      # baked ':' x and the mini-time baseline
WS_BTN_Y, WS_BTN_H = 1, 12          # windowshade mini-transport row
WS_BTNS = (('prev', 162, 11), ('play', 174, 12), ('pause', 187, 12),
           ('stop', 200, 11), ('next', 212, 12), ('eject', 225, 13))
MENU = QRect(6, 3, 9, 9)            # top-left main-menu (options) button
CLOSE = QRect(264, 3, 9, 9)
# Main title bar buttons: minimize (to taskbar) and windowshade (collapse).
MINIMIZE = QRect(244, 3, 9, 9)
SHADE_BTN = QRect(254, 3, 9, 9)
# EQ / playlist toggle buttons (from shufrep.bmp): open or close those panels.
EQ_TOGGLE = QRect(219, 58, 23, 12)
PL_TOGGLE = QRect(242, 58, 23, 12)
# Shuffle/repeat toggles (shufrep.bmp): off row y=0, on row y=30. Affect local
# playback only. Shuffle 47x15 at (28,y); Repeat 28x15 at (0,y).
SHUFFLE = QRect(164, 89, 47, 15)
REPEAT = QRect(210, 89, 28, 15)
VOLUME = QRect(107, 57, 68, 13)     # slider background area
VOL_HANDLE_W = 14
BALANCE = QRect(177, 57, 38, 13)    # stereo balance slider (right of volume)
BAL_HANDLE_W = 14
BAL_SNAP = 0.08                     # dead-zone that snaps the handle to center
TITLE_AREA = QRect(111, 27, 154, CHAR_H)   # song-title marquee (centered in the display box)
TIME_POS = [(48, 26), (60, 26), (78, 26), (90, 26)]  # MM:SS digit slots
MINUS_POS = (36, 26)                # leading "-" shown in remaining-time mode
TIME_RECT = QRect(36, 24, 66, 16)   # click to toggle elapsed <-> remaining
STATUS_POS = (26, 28)               # play/pause/stop indicator
# Clutterbar: the classic O/A/I/D/V strip Winamp draws on the display's left
# edge (the skin doesn't supply it). Each letter is a tiny clickable button.
CLUTTER_X = 10
CLUTTER = [('O', 22), ('A', 30), ('I', 38), ('D', 46), ('V', 54)]
VIS = QRect(22, 45, 78, 16)         # spectrum-analyzer visualization area (x=22, baseline y=60)
POSBAR = QRect(16, 72, 248, 10)     # song-progress bar (non-interactive); stretches
POSBAR_RIGHT_MARGIN = W - (POSBAR.x() + POSBAR.width())   # native gap to the right edge (11)
POSBAR_THUMB_W = 29                 # posbar.bmp: bg (0,0,248,10), thumb (248,0,29,10)
KBPS_POS = (111, 43)                 # bitrate number (small font, before "kbps")
KHZ_POS = (156, 43)                  # sample-rate number (before "kHz")
STEREO_POS = (239, 41)              # from monoster.bmp: (0,0) lit / (0,12) dim
MONO_POS = (212, 41)               # from monoster.bmp: (29,0) lit / (29,12) dim
VIS_BARS = 19                       # analyzer bars (76px / 4px pitch)
BAR_FALL = 0.09                     # per-frame bar falloff (rise is instant)
PEAK_GRAVITY = 0.007                # peak-cap fall acceleration (per frame^2)
WAVE_HARMONICS = 16                 # low spectrum bins summed into the scope wave

# Visualizer modes cycled by clicking the display / clutterbar 'V'.
VIS_BARS_MODE, VIS_LINES, VIS_DOTS, VIS_SCOPE, VIS_OFF = range(5)
VIS_MODES = 5

# Classic Winamp "nullsoft" easter egg: type n, u, l, Esc, l, Esc, s, o, f, t
# (i.e. "nullsoft" with Escape pressed after each 'l') to flash the llama line.
EGG_SEQUENCE = ('n', 'u', 'l', 'esc', 'l', 'esc', 's', 'o', 'f', 't')
EGG_MESSAGE = "IT REALLY WHIPS THE LLAMA'S ASS!"

# Keyboard map tuning (classic Winamp): Left/Right seek 5 s, Up/Down one notch.
SEEK_STEP_NS = 5_000_000_000     # 5 s per arrow-key seek
KEY_VOL_STEP = 1 / 27.0          # one volume-slider notch per arrow-key press


class SkinnedWindow(QWidget):
    def __init__(self, controller, skin, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint if parent is None else Qt.Widget)
        self.ctl = controller
        self.skin = skin
        self.text_font = TextFont(skin)
        self.num_font = NumberFont(skin)
        self.setFixedSize(W, H)
        self.setWindowTitle('Pyrrha')

        self._collapsed = False   # windowshade: collapsed to the title bar
        self._pressed = None      # currently-held transport button
        self._ws_pressed = None   # windowshade mini-transport button held
        self._ws_colon = None     # detected windowshade ':' (x, draw_own), cached
        self._cursor_name = False  # region cursor currently set (False = unknown)
        self._vol_dragging = False
        self._bal_dragging = False
        self._seek_dragging = False   # scrubbing the position bar (local files)
        self._seek_frac = 0.0         # preview fraction while scrubbing
        self._win_drag = None     # window-move offset
        self._scroll = 0          # marquee scroll offset
        self._time_remaining = False   # False = elapsed, True = remaining (-MM:SS)
        self._volume = float(controller.settings['volume'])
        self._balance = float(controller.settings['balance'])
        # Transient marquee readout (e.g. "Volume: 72%") shown while adjusting.
        self._readout = None
        self._readout_until = 0.0
        self._egg_progress = 0    # position in the "nullsoft" easter-egg sequence
        self._vis_colors = self._load_vis_colors()
        self._vis_active = False
        self._vis_mode = VIS_BARS_MODE      # click the display to cycle modes
        self._bar = [0.0] * VIS_BARS        # current bar heights (0..1)
        self._peak = [0.0] * VIS_BARS       # falling peak-cap positions
        self._peak_vel = [0.0] * VIS_BARS   # peak-cap velocities
        self._vis_edges_cache = None        # cached log-frequency bin edges
        self._wave = [0.0] * VIS.width()    # oscilloscope samples (-1..1)
        self._wave_ph = [i * 0.7 for i in range(WAVE_HARMONICS)]   # per-harmonic phase

        # Live updates from the controller.
        controller.song_changed.connect(lambda *_: self._reset_marquee())
        controller.play_state_changed.connect(lambda *_: self.update())
        controller.buffering_finished.connect(lambda *_: self.update())

        # Marquee scroll + time tick.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(220)

        # Faster tick for the spectrum-analyzer visualization.
        self._vis_timer = QTimer(self)
        self._vis_timer.timeout.connect(self._vis_tick)
        self._vis_timer.start(50)

    def _load_vis_colors(self):
        """Per-row analyzer colors (bottom → top) from the skin's VISCOLOR.TXT
        (entries 2..17), resampled to the visualization height; a green → red
        gradient is used when the skin has no palette. Also sets the peak-cap
        color (entry 23, the Winamp peak color)."""
        pal = []
        for line in self.skin.text('viscolor.txt').splitlines():
            parts = [x.strip() for x in line.split('//')[0].split(',')]
            if len(parts) >= 3 and parts[0]:
                try:
                    pal.append(QColor(int(parts[0]), int(parts[1]), int(parts[2])))
                except ValueError:
                    pass
        if len(pal) >= 18:
            grad = pal[2:18]
        else:
            grad = [QColor(int(255 * r / 15), int(255 * (1 - r / 15)), 40)
                    for r in range(16)]
        self._peak_color = pal[23] if len(pal) >= 24 else QColor(255, 255, 255)
        # Skin accent (analyzer base color) — used for the position-bar fill.
        self._accent = pal[2] if len(pal) >= 3 else QColor(31, 104, 236)
        h = VIS.height()
        return [grad[min(len(grad) - 1, r * len(grad) // h)] for r in range(h)]

    def _vis_edges(self, count):
        """Log-spaced FFT-bin edges grouping ``count`` spectrum bands into
        VIS_BARS display bars (cached per band count)."""
        if self._vis_edges_cache and self._vis_edges_cache[0] == count:
            return self._vis_edges_cache[1]
        edges = [min(count, max(1, int(round((count / 1.0) ** (i / VIS_BARS)))))
                 for i in range(VIS_BARS + 1)]
        for i in range(1, len(edges)):        # keep strictly increasing
            if edges[i] <= edges[i - 1]:
                edges[i] = min(count, edges[i - 1] + 1)
        self._vis_edges_cache = (count, edges)
        return edges

    def _advance_vis(self, raw):
        """Step the bar/peak animation one frame toward ``raw`` (the spectrum,
        or None to decay to silence). Returns True while anything is moving."""
        edges = self._vis_edges(len(raw)) if raw else None
        moving = False
        for i in range(VIS_BARS):
            if edges is not None:
                target = max(raw[edges[i]:edges[i + 1]] or raw[edges[i]:edges[i] + 1])
            else:
                target = 0.0
            # Bars rise instantly, fall gradually.
            if target >= self._bar[i]:
                self._bar[i] = target
            else:
                self._bar[i] = max(target, self._bar[i] - BAR_FALL)
            # Peak caps sit on new highs, then fall with gravity.
            if self._bar[i] >= self._peak[i]:
                self._peak[i] = self._bar[i]
                self._peak_vel[i] = 0.0
            else:
                self._peak_vel[i] += PEAK_GRAVITY
                self._peak[i] = max(0.0, self._peak[i] - self._peak_vel[i])
            if self._bar[i] > 0.001 or self._peak[i] > 0.001:
                moving = True
        return moving

    def _advance_wave(self, raw):
        """Step the oscilloscope one frame. Sums the low spectrum bands into a
        time-domain-ish waveform: its amplitude tracks loudness directly (louder
        music = bigger swings) and every band's phase advances each frame, so the
        trace keeps moving even between spectrum updates. tanh soft-clips it for a
        natural scope swing. Decays to a flat line when silent. Returns True while
        anything is moving."""
        n = len(self._wave)
        if raw:
            amps = raw[:WAVE_HARMONICS]
            two_pi = 2.0 * math.pi
            for i in range(len(amps)):
                # Fast, spread-out (incommensurate) motion so successive frames
                # look different rather than a slowly morphing standing wave.
                self._wave_ph[i] = (self._wave_ph[i] + 0.20 + 0.10 * i) % two_pi
            for x in range(n):
                t = x / n
                s = 0.0
                for i, a in enumerate(amps):
                    s += a * math.sin(two_pi * (i + 1) * t + self._wave_ph[i])
                self._wave[x] = math.tanh(s * 0.8)    # dynamics straight from loudness
            return True
        moving = False
        for x in range(n):
            self._wave[x] *= 0.82
            if abs(self._wave[x]) > 0.002:
                moving = True
        return moving

    def _vis_tick(self):
        if self._vis_mode == VIS_OFF:      # visualizer off — no repaints needed
            return
        playing = self.ctl.playing
        raw = getattr(self.ctl, 'spectrum_bands', None) if playing else None
        if self._vis_mode == VIS_SCOPE:
            moving = self._advance_wave(raw)
        else:
            moving = self._advance_vis(raw)
        if playing or moving or self._vis_active:
            self.update()
        self._vis_active = playing or moving

    # ---------------------------------------------------------------- helpers
    def set_skin(self, skin):
        self.skin = skin
        self._ws_colon = None
        self._cursor_name = False   # force re-evaluation against the new skin
        self.unsetCursor()
        self.text_font = TextFont(skin)
        self.num_font = NumberFont(skin)
        self._vis_colors = self._load_vis_colors()   # also refreshes accent/peak
        self.update()

    # -------------------------------------------------------------- cursors
    def _cursor_for(self, pos):
        """The skin cursor filename for the region at ``pos`` (classic Winamp
        gives each region its own pointer), or None for the default arrow."""
        if self._collapsed:
            return 'titlebar.cur'          # the windowshade is all title bar
        dx = self._dx()
        if MENU.contains(pos):
            return 'mainmenu.cur'
        for r in (CLOSE, MINIMIZE, SHADE_BTN):
            if r.translated(dx, 0).contains(pos):
                return 'close.cur'
        if pos.y() < TITLEBAR[3]:           # title bar, not over a window button
            return 'titlebar.cur'
        if POSBAR.adjusted(0, 0, dx, 0).contains(pos):   # seek bar stretches
            return 'posbar.cur'
        if VOLUME.contains(pos) or BALANCE.contains(pos):
            return 'volbal.cur'
        return None

    def _update_cursor(self, pos):
        """Swap in the region's skin cursor when it changes. No-op for skins that
        ship no cursors (the common case), which keep the default arrow."""
        if not self.skin.has_cursors:
            return
        name = self._cursor_for(pos)
        if name == self._cursor_name:
            return
        self._cursor_name = name
        cur = self.skin.cursor(name) if name else None
        self.setCursor(cur) if cur is not None else self.unsetCursor()

    def _load_skin_file(self):
        path, _sel = QFileDialog.getOpenFileName(
            self, _('Load Winamp Skin'), self.ctl.skins_dir(),
            _('Winamp skins (*.wsz *.zip);;All files (*)'))
        if path:
            self.window().load_skin(path)

    def _load_skin_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, _('Load Skin Folder'), self.ctl.skins_dir())
        if path:
            self.window().load_skin(path)

    def _title_text(self):
        if self._readout and time.monotonic() < self._readout_until:
            return self._readout
        song = self.ctl.current_song
        if song is None:
            return 'PYRRHA'
        return '{} - {}'.format(song.artist, song.title)

    def _show_readout(self, text, secs=1.2):
        """Briefly replace the title marquee with a status line (volume/balance),
        the way Winamp flashes the level while you drag a slider."""
        self._readout = text
        self._readout_until = time.monotonic() + secs
        self._reset_marquee()   # show it from the start, un-scrolled

    def egg_key(self, event):
        """Feed a key press to the classic 'nullsoft' easter egg matcher. Returns
        True if the key was consumed by the sequence (matched or restarted it)."""
        if event.key() == Qt.Key_Escape:
            token = 'esc'
        else:
            text = event.text().lower()
            if len(text) != 1 or not text.isalpha():
                self._egg_progress = 0
                return False
            token = text
        if token == EGG_SEQUENCE[self._egg_progress]:
            self._egg_progress += 1
            if self._egg_progress == len(EGG_SEQUENCE):
                self._egg_progress = 0
                self._show_readout(EGG_MESSAGE, secs=3.0)
            return True
        # Mismatch: reset, but let this key start a fresh sequence.
        self._egg_progress = 1 if token == EGG_SEQUENCE[0] else 0
        return self._egg_progress == 1

    def _reset_marquee(self):
        self._scroll = 0
        self.update()

    def _tick(self):
        self._scroll += 1
        self.update()

    def _time_display(self):
        """(MMSS string, is_remaining). Remaining shows a leading '-'."""
        pos = self.ctl.query_position()
        if pos is None:
            return '    ', False
        secs = int(pos // 1_000_000_000)
        if self._time_remaining:
            dur = self.ctl.query_duration()
            if dur and dur > 0:
                secs = max(0, int(dur // 1_000_000_000) - secs)
                return '{:02d}{:02d}'.format((secs // 60) % 100, secs % 60), True
        return '{:02d}{:02d}'.format((secs // 60) % 100, secs % 60), False

    def _status_sprite_x(self):
        if not self.ctl.playing:
            return 9   # pause
        return 0       # play

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event):
        p = QPainter(self)
        s = self._scale()
        if s != 1:
            p.scale(s, s)   # everything below is drawn in logical coords

        lw = self._lw()
        if self._collapsed:   # windowshade: compact title + time display
            self._paint_windowshade(p, lw)
            p.end()
            return
        gap = lw - W        # extra width to fill (0 at native size)
        dx = gap            # right-anchored controls shift by this much
        rkeep = W - SPLIT   # width of the right slice, anchored to the right edge

        # Background. At native width (classic mode is fixed-size + zoom only)
        # blit main.bmp 1:1 so nothing is sliced. Only Modern's widen stretches
        # it: a fixed left slice, the skin's own pixels tiled across the gap (a
        # 1px column tiles cleanly since the face runs horizontally), then the
        # fixed right slice anchored to the right edge.
        if gap:
            p.drawImage(0, 0, self.skin.sprite('main.bmp', 0, 0, SPLIT, H))
            p.drawImage(QRect(SPLIT, 0, gap, H),
                        self.skin.sprite('main.bmp', SPLIT - 1, 0, 1, H))
            p.drawImage(lw - rkeep, 0, self.skin.sprite('main.bmp', SPLIT, 0, rkeep, H))
        else:
            p.drawImage(0, 0, self.skin.sprite('main.bmp', 0, 0, W, H))

        # Title bar: tile the fill across the full width, keep the end corners
        # (which carry the menu/close/minimize glyphs), and re-center the title
        # graphic so it stays centered as the window widens.
        self._paint_titlebar(p, lw)

        # Playback status indicator (left-anchored).
        p.drawImage(STATUS_POS[0], STATUS_POS[1],
                    self.skin.sprite('playpaus.bmp', self._status_sprite_x(), 0, 9, 9))

        # Clutterbar (classic Winamp O/A/I/D/V strip on the display's left edge).
        if getattr(self.window(), 'mode', 'modern') == 'classic':
            self._paint_clutterbar(p)

        # Time (MM SS) (left-anchored); a leading "-" marks remaining time.
        tstr, remaining = self._time_display()
        for (x, y), ch in zip(TIME_POS, tstr):
            p.drawImage(x, y, self.num_font.digit(ch))
        if remaining:
            p.drawImage(MINUS_POS[0], MINUS_POS[1], self.num_font.digit('-'))

        # Song-title marquee (clipped, scrolling) — widened to fill the gap.
        self._paint_marquee(p, gap)

        # Spectrum-analyzer visualization (left-anchored).
        self._paint_visualizer(p)

        # Bitrate / sample-rate / stereo indicators (left-anchored).
        self._paint_stream_info(p)

        # Song-progress bar. Draggable to seek for local files; Pandora streams
        # aren't seekable, so it's display-only there (see ctl.seekable()).
        self._paint_position(p)

        # Volume: filled background frame + handle (left-anchored).
        level = min(27, max(0, round(self._volume * 27)))
        p.drawImage(VOLUME.x(), VOLUME.y(),
                    self.skin.sprite('volume.bmp', 0, level * 15, 68, 13))
        hx = VOLUME.x() + round(self._volume * (VOLUME.width() - VOL_HANDLE_W))
        p.drawImage(hx, VOLUME.y() + 1,
                    self.skin.sprite('volume.bmp',
                                     0 if self._vol_dragging else 15, 422, VOL_HANDLE_W, 11))

        # Balance: background frame (row 0 = centered) + handle (left-anchored).
        if self.skin.has('balance.bmp'):
            blvl = min(27, round(abs(self._balance) * 27))
            p.drawImage(BALANCE.x(), BALANCE.y(),
                        self.skin.sprite('balance.bmp', 9, blvl * 15, 38, 13))
            bhx = BALANCE.x() + round((self._balance + 1) / 2 * (BALANCE.width() - BAL_HANDLE_W))
            p.drawImage(bhx, BALANCE.y() + 1,
                        self.skin.sprite('balance.bmp',
                                         0 if self._bal_dragging else 15, 422, BAL_HANDLE_W, 11))

        # Transport buttons (left-anchored).
        for name, bx, by, w, h, sx, sy_n, sy_p in BUTTONS:
            sy = sy_p if self._pressed == name else sy_n
            p.drawImage(bx, by, self.skin.sprite('cbuttons.bmp', sx, sy, w, h))

        # EQ / playlist toggle buttons (right-anchored, lit when the panel is open).
        shell = self.window()
        for panel, rect, sx in ((getattr(shell, 'eq', None), EQ_TOGGLE, 0),
                                (getattr(shell, 'pl', None), PL_TOGGLE, 23)):
            if panel is not None:
                sy = 73 if panel._closed else 61   # off / on
                p.drawImage(rect.x() + dx, rect.y(),
                            self.skin.sprite('shufrep.bmp', sx, sy, 23, 12))

        # Shuffle / repeat toggles (lit when on). Play order for local files.
        p.drawImage(SHUFFLE.x(), SHUFFLE.y(), self.skin.sprite(
            'shufrep.bmp', 28, 30 if getattr(self.ctl, 'shuffle', False) else 0, 47, 15))
        p.drawImage(REPEAT.x(), REPEAT.y(), self.skin.sprite(
            'shufrep.bmp', 0, 30 if getattr(self.ctl, 'repeat', False) else 0, 28, 15))
        p.end()

    def _paint_windowshade(self, p, lw):
        """Winamp windowshade: the compact titlebar with the song title scrolling
        in its display and the elapsed time on the baked ':'. The skin bakes the
        transport/buttons; a wider (Modern) shade just shows the normal titlebar."""
        if lw != W:                       # only the native compact bar has the display
            self._paint_titlebar(p, lw)
            return
        ws_ty = WS_TB_ACTIVE if self.isActiveWindow() else WS_TB_INACTIVE
        p.drawImage(0, 0, self.skin.sprite('titlebar.bmp', 27, ws_ty, W, 14))
        # Scrolling title over the skin's own display area (its bg shows through
        # the font's transparency, so it looks native on light and dark shades).
        tx, ty, tw = WS_TITLE
        img = self.text_font.render(self._title_text() + '   ***   ')
        p.save()
        p.setClipRect(tx, ty, tw, CHAR_H)
        if img.width() > tw:
            span = img.width()
            off = self._scroll % span
            p.drawImage(tx - off, ty, img)
            p.drawImage(tx - off + span, ty, img)
        else:
            p.drawImage(tx, ty, img)
        p.restore()
        # Elapsed time as digits straddling the ':'. We always draw our own ':'
        # at the (detected) slot x: where the skin bakes one it aligns and reads
        # as a single ':'; where it doesn't (some skins), ours fills it in.
        tstr, _rem = self._time_display()
        mm, ss = (tstr[:2].lstrip('0') or '0'), tstr[2:]
        if ss.strip():
            colon_x = self._ws_colon_x()
            x = colon_x - 2
            for ch in reversed(mm):
                x -= CHAR_W
                p.drawImage(x, WS_TIME_Y, self.text_font.render(ch))
            p.drawImage(colon_x - 2, WS_TIME_Y, self.text_font.render(':'))
            x = colon_x + 3
            for ch in ss:
                p.drawImage(x, WS_TIME_Y, self.text_font.render(ch))
                x += CHAR_W
        # Pressed-state feedback for a mini-transport button (baked glyphs).
        if self._ws_pressed:
            for name, bx, bw in WS_BTNS:
                if name == self._ws_pressed:
                    p.fillRect(bx, WS_BTN_Y, bw, WS_BTN_H, QColor(0, 0, 0, 90))
                    break

    def _ws_colon_x(self):
        """The x of the windowshade time ':' — the skin's baked one when there's a
        clear tight feature at the standard slot, else the standard x. Cached; we
        draw our own ':' there regardless, so this only fine-tunes alignment."""
        if self._ws_colon is None:
            colon_x = WS_COLON_X
            s = self.skin.sprite('titlebar.bmp', 27, WS_TB_ACTIVE, W, 14)
            if not s.isNull():
                def contrast(x):
                    band = [(s.pixelColor(x, y).red() + s.pixelColor(x, y).green()
                             + s.pixelColor(x, y).blue()) // 3 for y in range(2, 12)]
                    return max(band) - min(band)
                peak = max(range(WS_COLON_X - 3, WS_COLON_X + 4), key=contrast)
                nb = sorted(contrast(WS_COLON_X + d) for d in (-8, -7, -6, 6, 7, 8))
                if contrast(peak) >= nb[len(nb) // 2] + 25:   # a clear standout ':'
                    colon_x = peak
            self._ws_colon = colon_x
        return self._ws_colon

    def _paint_titlebar(self, p, lw):
        ty = 0 if self.isActiveWindow() else 15
        # At native width, blit the whole title bar 1:1 so every glyph (menu,
        # minimize, windowshade, close) lands at its skin position. Only Modern's
        # widen needs the stretch: tile the fill, keep the end corners, and
        # re-center the title graphic as the bar grows.
        if lw == W:
            p.drawImage(0, 0, self.skin.sprite('titlebar.bmp', 27, ty, W, 14))
            return
        p.drawImage(QRect(0, 0, lw, 14),
                    self.skin.sprite('titlebar.bmp', 27 + TB_CORNER + 1, ty, 1, 14))
        p.drawImage(0, 0, self.skin.sprite('titlebar.bmp', 27, ty, TB_CORNER, 14))
        p.drawImage(lw - TB_CORNER, 0,
                    self.skin.sprite('titlebar.bmp', 27 + W - TB_CORNER, ty, TB_CORNER, 14))
        p.drawImage((lw - TB_TITLE) // 2, 0,
                    self.skin.sprite('titlebar.bmp', 27 + (W - TB_TITLE) // 2, ty, TB_TITLE, 14))

    def _paint_marquee(self, p, gap):
        img = self.text_font.render(self._title_text() + '   ***   ')
        area = QRect(TITLE_AREA.x(), TITLE_AREA.y(),
                     TITLE_AREA.width() + gap, TITLE_AREA.height())
        p.save()
        p.setClipRect(area)
        span = max(img.width(), area.width())
        off = self._scroll % span if img.width() > area.width() else 0
        p.drawImage(area.x() - off, area.y(), img)
        if off:  # wrap-around copy
            p.drawImage(area.x() - off + span, area.y(), img)
        p.restore()

    def _posbar_geom(self):
        # Stretch the bar to the window width, keeping the native right margin.
        x, y, h = POSBAR.x(), POSBAR.y(), POSBAR.height()
        bar_w = max(1, self._lw() - x - POSBAR_RIGHT_MARGIN)
        return x, y, bar_w, h

    def _paint_position(self, p):
        x, y, bar_w, h = self._posbar_geom()
        p.drawImage(QRect(x, y, bar_w, h), self.skin.sprite('posbar.bmp', 0, 0, 248, 10))
        if self._seek_dragging:
            frac = self._seek_frac        # follow the cursor while scrubbing
        else:
            pos = self.ctl.query_position()
            dur = self.ctl.query_duration()
            if not (pos and dur and dur > 0):
                return
            frac = min(1.0, max(0.0, pos / dur))
        if getattr(self.window(), 'mode', 'modern') == 'classic':
            # Authentic Winamp slider knob from the skin.
            tx = x + int(frac * (bar_w - POSBAR_THUMB_W))
            p.drawImage(tx, y, self.skin.sprite('posbar.bmp', 248, 0, POSBAR_THUMB_W, h))
        else:
            # Modern: a blue (accent) progress fill with a bright leading cap.
            fill_w = int(frac * (bar_w - 2))
            if fill_w <= 0:
                return
            r = QRect(x + 1, y + 2, fill_w, h - 4)
            p.fillRect(r, self._accent)
            p.fillRect(QRect(r.right() - 1, r.y(), 2, r.height()), self._peak_color)

    def _paint_clutterbar(self, p):
        f = QFont()
        f.setPixelSize(6)
        f.setBold(True)
        p.setFont(f)
        shell = self.window()
        active = {'A': getattr(shell, 'keep_above', False),
                  'D': getattr(shell, 'scale', 1) > 1}
        for label, y in CLUTTER:
            p.setPen(QColor(210, 212, 220) if active.get(label) else QColor(96, 98, 108))
            p.drawText(QRect(CLUTTER_X, y, 7, 8), Qt.AlignCenter, label)

    def _clutter_action(self, label, gpos):
        c, shell = self.ctl, self.window()
        if label == 'O':                       # Options
            self._show_menu(gpos)
        elif label == 'A':                     # Always on top (KWin "keep above")
            shell.keep_above = not shell.keep_above
            if not self._kwin_keep_above(shell.keep_above):
                shell.setWindowFlag(Qt.WindowStaysOnTopHint, shell.keep_above)
                shell.show()                   # fallback: Qt hint + re-map
            self.update()
        elif label == 'I' and hasattr(c, 'info_song'):   # song Info
            c.info_song()
        elif label == 'D':                     # Size: cycle 1x -> 1.5x -> 2x
            cur = getattr(shell, 'scale', 1.0)
            shell.set_scale(1.5 if cur < 1.25 else 2.0 if cur < 1.75 else 1.0)
        elif label == 'V':                     # Visualization mode
            self._vis_mode = (self._vis_mode + 1) % VIS_MODES
            self.update()

    def _set_time_remaining(self, remaining):
        """Set the display's elapsed/remaining mode (from the Options menu)."""
        self._time_remaining = bool(remaining)
        self.update()

    def _set_vis_mode(self, mode):
        """Set the visualization mode directly (from the Options menu)."""
        self._vis_mode = mode
        self.update()

    def _kwin_keep_above(self, above):
        """Set the window's keep-above state through KWin's D-Bus scripting
        interface (works on Wayland where the Qt hint is ignored). Returns True
        on success. Matches our window by app-id or caption."""
        import os
        import tempfile
        try:
            from gi.repository import Gio, GLib
        except Exception:
            return False
        # Match by app-id (resourceClass contains "pyrrha") or an exact caption of
        # "Pyrrha". The caption must be exact — a substring test also flags
        # unrelated windows that merely mention Pyrrha (e.g. a terminal whose title
        # is "Pyrrha : … — Konsole").
        js = ('(function(){\n'
              '  var l=(typeof workspace.windowList==="function")?workspace.windowList()\n'
              '        :(typeof workspace.clientList==="function")?workspace.clientList():[];\n'
              '  for(var i=0;i<l.length;i++){var c=l[i];\n'
              '    var rc=((c.resourceClass!=null)?c.resourceClass:"").toString().toLowerCase();\n'
              '    var cap=((c.caption!=null)?c.caption:"").toString();\n'
              '    if(rc.indexOf("pyrrha")!==-1||cap==="Pyrrha"){c.keepAbove=%s;}\n'
              '  }})();\n') % ('true' if above else 'false')
        fd, path = tempfile.mkstemp(suffix='.js', prefix='pyrrha-ka-')
        with os.fdopen(fd, 'w') as f:
            f.write(js)
        name = os.path.basename(path)
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            sid = None
            for sig, args in (('(ss)', (path, name)), ('(s)', (path,))):
                try:
                    r = bus.call_sync('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting',
                                      'loadScript', GLib.Variant(sig, args),
                                      GLib.VariantType('(i)'), Gio.DBusCallFlags.NONE, 2000, None)
                    sid = r.unpack()[0]
                    break
                except GLib.Error:
                    continue
            if sid is None or sid < 0:
                return False
            bus.call_sync('org.kde.KWin', '/Scripting/Script%d' % sid, 'org.kde.kwin.Script',
                          'run', None, None, Gio.DBusCallFlags.NONE, 2000, None)
            try:
                bus.call_sync('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting',
                              'unloadScript', GLib.Variant('(s)', (name,)),
                              None, Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass
            return True
        except GLib.Error as e:
            logging.debug('KWin keep-above via D-Bus failed: %s', e)
            return False
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _paint_stream_info(self, p):
        bitrate, rate, channels = self.ctl.audio_stream_info()
        if bitrate:
            p.drawImage(KBPS_POS[0], KBPS_POS[1], self.text_font.render('%3d' % bitrate))
        if rate:
            p.drawImage(KHZ_POS[0], KHZ_POS[1],
                        self.text_font.render('%2d' % round(rate / 1000)))
        stereo_y = 0 if channels == 2 else 12    # lit / dim
        mono_y = 0 if channels == 1 else 12
        p.drawImage(STEREO_POS[0], STEREO_POS[1],
                    self.skin.sprite('monoster.bmp', 0, stereo_y, 29, 12))
        p.drawImage(MONO_POS[0], MONO_POS[1],
                    self.skin.sprite('monoster.bmp', 29, mono_y, 27, 12))

    def _paint_scope(self, p):
        """Oscilloscope: a waveform line across the visualization area. Colored
        from the same palette as the analyzer bars (the active skin's VISCOLOR
        gradient) so it matches the current theme — a bigger excursion uses a
        "hotter" color, the way a taller bar does."""
        colors = self._vis_colors
        last = len(colors) - 1
        mid = VIS.y() + VIS.height() // 2
        amp = (VIS.height() - 1) / 2.0
        prev_y = None
        for x in range(VIS.width()):
            v = self._wave[x]
            y = mid - int(round(v * amp))
            y = max(VIS.y(), min(VIS.bottom(), y))
            color = colors[min(last, int(abs(v) * (last + 1)))]
            px = VIS.x() + x
            if prev_y is None:
                p.fillRect(px, y, 1, 1, color)
            else:                      # join to the previous sample for a solid trace
                lo, hi = (prev_y, y) if prev_y <= y else (y, prev_y)
                p.fillRect(px, lo, 1, hi - lo + 1, color)
            prev_y = y

    def _paint_visualizer(self, p):
        if self._vis_mode == VIS_OFF:
            return
        if self._vis_mode == VIS_SCOPE:
            self._paint_scope(p)
            return
        colors = self._vis_colors
        top_row = len(colors) - 1
        bar_w = max(1, VIS.width() // VIS_BARS)
        w = max(1, bar_w - 1)          # 1px gap between bars
        bottom = VIS.bottom()
        height = VIS.height()
        for i in range(VIS_BARS):
            x = VIS.x() + i * bar_w
            h = int(round(self._bar[i] * height))
            if self._vis_mode == VIS_LINES:    # thin lines (1px wide)
                for row in range(h):
                    p.fillRect(x, bottom - row, 1, 1, colors[min(row, top_row)])
            elif self._vis_mode == VIS_DOTS:   # dots (top of each bar only)
                if h > 0:
                    p.fillRect(x, bottom - (h - 1), w, 1, colors[min(h - 1, top_row)])
            else:                      # bars (filled) with a falling peak cap
                for row in range(h):
                    p.fillRect(x, bottom - row, w, 1, colors[min(row, top_row)])
                pr = int(round(self._peak[i] * (height - 1)))
                if pr > 0:
                    p.fillRect(x, bottom - pr, w, 1, self._peak_color)

    # ------------------------------------------------------------------ mouse
    def _button_at(self, pos):
        for name, dx, dy, w, h, *_ in BUTTONS:
            if QRect(dx, dy, w, h).contains(pos):
                return name
        return None

    def _ws_transport_at(self, pos):
        """Which windowshade mini-transport button is at ``pos``, or None."""
        if WS_BTN_Y <= pos.y() <= WS_BTN_Y + WS_BTN_H:
            for name, bx, bw in WS_BTNS:
                if bx <= pos.x() < bx + bw:
                    return name
        return None

    def _do_ws_transport(self, name):
        # Reuse the normal transport logic so behaviour stays in lockstep. Only
        # Pandora-mode eject needs a shade-specific anchor for its station menu
        # (the full-window eject button is hidden while collapsed).
        if name == 'eject' and not self.ctl.local_mode:
            s = self._scale()
            for n, bx, bw in WS_BTNS:
                if n == 'eject':
                    self._show_stations_menu(self.mapToGlobal(
                        QPoint(int(bx * s), int((WS_BTN_Y + WS_BTN_H) * s))))
                    break
            return
        self._activate(name)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        # A click that merely activates an unfocused window shouldn't fire the
        # press-sensitive controls (transport, seek, volume/balance) — but the
        # low-risk toggles handled below (menu/close/minimize/shade, EQ/PL,
        # clutterbar incl. always-on-top, shuffle/repeat) must still act on that
        # first click. Otherwise e.g. always-on-top never toggles: you click it
        # exactly when the window is behind another, and the guard eats the click.
        shell = self.window()
        focus_click = bool(getattr(shell, 'is_focus_click', lambda: False)())
        pos = (event.position() / self._scale()).toPoint()   # logical coords
        dx = self._dx()
        if MENU.contains(pos):
            self._show_menu(event.globalPosition().toPoint())
            return
        if CLOSE.translated(dx, 0).contains(pos):
            self.ctl.quit()
            return
        if MINIMIZE.translated(dx, 0).contains(pos):
            self.window().showMinimized()
            return
        if SHADE_BTN.translated(dx, 0).contains(pos):
            self._toggle_shade()
            return
        if self._collapsed:                    # windowshade mini-transport
            wt = self._ws_transport_at(pos)
            if wt is not None:
                self._ws_pressed = wt
                self.update()
                self._do_ws_transport(wt)
                return
        if EQ_TOGGLE.translated(dx, 0).contains(pos):
            self.window().toggle_panel(getattr(self.window(), 'eq', None))
            return
        if PL_TOGGLE.translated(dx, 0).contains(pos):
            self.window().toggle_panel(getattr(self.window(), 'pl', None))
            return
        if TIME_RECT.contains(pos):        # toggle elapsed <-> remaining
            self._time_remaining = not self._time_remaining
            self.update()
            return
        if VIS.contains(pos):              # cycle visualizer mode
            self._vis_mode = (self._vis_mode + 1) % VIS_MODES
            self.update()
            return
        if getattr(self.window(), 'mode', 'modern') == 'classic':
            for label, cy in CLUTTER:      # clutterbar buttons
                if QRect(CLUTTER_X, cy, 8, 8).contains(pos):
                    self._clutter_action(label, event.globalPosition().toPoint())
                    return
        if SHUFFLE.contains(pos):
            self.ctl.toggle_shuffle()
            self.update()
            return
        if REPEAT.contains(pos):
            self.ctl.toggle_repeat()
            self.update()
            return
        # Below here are the press-sensitive controls: a bare focus-click must
        # not fire them (e.g. accidentally hit Stop or jump the seek bar).
        if focus_click:
            return
        name = self._button_at(pos)
        if name:
            self._pressed = name
            self.update()
            return
        if VOLUME.adjusted(0, -2, 0, 2).contains(pos):
            self._vol_dragging = True
            self._set_volume_from_x(pos.x())
            return
        if self.skin.has('balance.bmp') and BALANCE.adjusted(0, -2, 0, 2).contains(pos):
            self._bal_dragging = True
            self._set_balance_from_x(pos.x())
            return
        if self.ctl.seekable():
            bx, by, bar_w, bh = self._posbar_geom()
            if QRect(bx, by, bar_w, bh).adjusted(0, -2, 0, 2).contains(pos):
                self._seek_dragging = True
                self._seek_frac = self._seek_frac_from_x(pos.x())
                self.update()
                return
        if pos.y() < TITLEBAR[3]:   # drag by the title bar
            shell = self.window()
            # Classic is a screen-sized overlay: dragging main moves the whole
            # player (all panels) within it. Modern moves the shell window itself.
            if getattr(shell, 'mode', 'modern') == 'classic':
                self._titledrag = True
                shell.start_free_drag(self, event.globalPosition().toPoint())
            else:
                handle = shell.windowHandle()
                if handle is not None:
                    handle.startSystemMove()   # compositor-driven move (Wayland/X11)

    def mouseMoveEvent(self, event):
        if getattr(self, '_titledrag', False):
            self.window().free_drag_move(self, event.globalPosition().toPoint())
            return
        if self._vol_dragging:
            self._set_volume_from_x((event.position() / self._scale()).x())
        elif self._bal_dragging:
            self._set_balance_from_x((event.position() / self._scale()).x())
        elif self._seek_dragging:
            self._seek_frac = self._seek_frac_from_x((event.position() / self._scale()).x())
            self.update()
        else:
            self._update_cursor((event.position() / self._scale()).toPoint())

    def mouseReleaseEvent(self, event):
        if self._ws_pressed:                   # release windowshade mini-transport
            self._ws_pressed = None
            self.update()
        if getattr(self, '_titledrag', False):
            self._titledrag = False
            self.window().end_free_drag()
            return
        pos = (event.position() / self._scale()).toPoint()   # logical coords
        if self._pressed:
            if self._button_at(pos) == self._pressed:
                self._activate(self._pressed)
            self._pressed = None
            self.update()
        if self._seek_dragging:
            dur = self.ctl.query_duration()
            if dur:
                self.ctl.seek(int(self._seek_frac * dur))
            self._seek_dragging = False
            self.update()
        self._vol_dragging = False
        self._bal_dragging = False
        self._win_drag = None

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = (event.position() / self._scale()).toPoint()
        if VIS.contains(pos):                  # double-click the vis → big window
            self.window().open_vis_window()
            return
        if pos.y() >= TITLEBAR[3]:
            return
        dx = self._dx()
        # Double-clicking the title bar (but not its buttons) toggles windowshade.
        for r in (MENU, CLOSE.translated(dx, 0), MINIMIZE.translated(dx, 0),
                  SHADE_BTN.translated(dx, 0)):
            if r.contains(pos):
                return
        self._toggle_shade()

    def _set_volume_from_x(self, x):
        frac = (x - VOLUME.x()) / max(1, VOLUME.width() - VOL_HANDLE_W)
        self._set_volume(frac)

    def _set_balance_from_x(self, x):
        frac = (x - BALANCE.x()) / max(1, BALANCE.width() - BAL_HANDLE_W)
        self._set_balance(frac * 2.0 - 1.0)

    def _seek_frac_from_x(self, x):
        bx, _y, bar_w, _h = self._posbar_geom()
        return min(1.0, max(0.0, (x - bx) / max(1, bar_w)))

    def _set_volume(self, frac):
        self._volume = min(1.0, max(0.0, frac))
        self.ctl.set_player_volume(self._volume)
        self.ctl.settings.set_double('volume', self._volume)
        self._show_readout(_('Volume: {}%').format(round(self._volume * 100)))
        self.update()

    def _set_balance(self, value):
        value = max(-1.0, min(1.0, value))
        if abs(value) < BAL_SNAP:      # dead-zone snap to dead-center
            value = 0.0
        self._balance = value
        self.ctl.set_player_balance(value)
        self.ctl.settings.set_double('balance', value)
        if value == 0.0:
            self._show_readout(_('Balance: center'))
        else:
            side = _('right') if value > 0 else _('left')
            self._show_readout(_('Balance: {}% {}').format(round(abs(value) * 100), side))
        self.update()

    def change_volume(self, delta):
        self._set_volume(self._volume + delta)

    def contextMenuEvent(self, event):
        self._show_menu(event.globalPos())   # right-click anywhere -> main menu

    def wheelEvent(self, event):
        self.window().wheelEvent(event)      # scroll -> volume (handled by the shell)

    def _activate(self, name):
        c = self.ctl
        if name == 'play':
            c.user_play()
        elif name == 'pause':
            c.user_pause()
        elif name == 'stop':
            c.stop()
        elif name == 'next':
            c.next_song()
        elif name == 'prev':
            c.prev_song()   # local playback only; no-op for Pandora
        elif name == 'eject':
            # Winamp's Eject loads a file; in local mode open files, otherwise
            # pop the station switcher below the eject button.
            if c.local_mode:
                c.open_local_files()
            else:
                s = self._scale()
                for n, dx, dy, w, h, *_ in BUTTONS:
                    if n == 'eject':
                        self._show_stations_menu(
                            self.mapToGlobal(QPoint(int(dx * s), int((dy + h) * s))))
                        break

    def _populate_stations(self, menu):
        """Fill a menu with the user's stations; picking one switches to it."""
        c = self.ctl
        rows = getattr(c, 'stations_model', None) or []
        if not rows:
            act = menu.addAction(_('(no stations loaded yet)'))
            act.setEnabled(False)
            return
        for station, name, index in rows:
            act = menu.addAction(name, lambda *a, s=station: c.station_changed(s))
            act.setCheckable(True)
            act.setChecked(station is c.current_station)

    def _show_stations_menu(self, global_pos):
        menu = QMenu(self)
        self._populate_stations(menu)
        menu.addSeparator()
        menu.addAction(_('Manage Stations…'), self.ctl.show_stations)
        menu.exec(global_pos)

    def _show_menu(self, global_pos):
        c = self.ctl
        radio_mode = getattr(c, 'is_radio', False)
        menu = QMenu(self)
        # Source switch: Pandora / Local Playback / Internet Radio. Each mode
        # offers a route to the other two, plus its own actions (local files,
        # Pandora stations, or the radio browser).
        if c.local_mode:
            menu.addAction(_('Switch to Pandora'), c.switch_to_pandora)
            menu.addAction(_('Internet Radio…'), c.show_radio)
            menu.addSeparator()
            menu.addAction(_('Open Files…'), c.open_local_files)
            menu.addAction(_('Open Folder…'), c.open_local_folder)
            menu.addAction(_('Open Playlist…'), c.open_playlist)
            save = menu.addAction(_('Save Playlist…'), c.save_playlist)
            save.setEnabled(len(c.songs_model) > 0)
        elif radio_mode:
            menu.addAction(_('Browse Stations…'), c.show_radio)
            menu.addSeparator()
            menu.addAction(_('Switch to Pandora'), c.switch_to_pandora)
            menu.addAction(_('Switch to Local Playback'), c.switch_to_local)
        else:
            menu.addAction(_('Switch to Local Playback'), c.switch_to_local)
            menu.addAction(_('Internet Radio…'), c.show_radio)
            menu.addSeparator()
            stations = menu.addMenu(_('Stations'))
            self._populate_stations(stations)
            stations.addSeparator()
            stations.addAction(_('Manage Stations…'), c.show_stations)
        menu.addSeparator()
        menu.addAction(_('Preferences…'), c.show_preferences)
        menu.addAction(_('About Pyrrha'), c.show_about)
        if not c.local_mode and not radio_mode:   # ratings are Pandora-only
            menu.addSeparator()
            menu.addAction(_('Love'), lambda: c.love_song())
            menu.addAction(_('Ban'), lambda: c.ban_song())
            menu.addAction(_('Tired'), lambda: c.tired_song())
        menu.addSeparator()
        # Jump/File Info/Shuffle/Repeat apply to the static local playlist only
        # — the Pandora queue can't be rewound, searched by file, tagged, or
        # reordered — so they're omitted entirely in Pandora mode.
        if c.local_mode:
            # Jump to a track by typing (classic Winamp 'J').
            jump = menu.addAction(_('Jump to File…') + '\tJ',
                                  lambda: self.window().open_jump_to_file())
            jump.setEnabled(len(c.songs_model) > 0)
            jump_time = menu.addAction(_('Jump to Time…') + '\tCtrl+J',
                                       lambda: self.window().open_jump_to_time())
            jump_time.setEnabled(c.seekable())
            if hasattr(c, 'info_song'):
                info = menu.addAction(_('File Info…') + '\tAlt+3', lambda: c.info_song())
                info.setEnabled(c.current_song is not None)
            # Playback toggles — Options-menu parity with the sprites/clutterbar.
            shuffle = menu.addAction(_('Shuffle') + '\tS',
                                     lambda: (c.toggle_shuffle(), self.update()))
            shuffle.setCheckable(True)
            shuffle.setChecked(bool(getattr(c, 'shuffle', False)))
            repeat = menu.addAction(_('Repeat') + '\tR',
                                    lambda: (c.toggle_repeat(), self.update()))
            repeat.setCheckable(True)
            repeat.setChecked(bool(getattr(c, 'repeat', False)))
        # Sleep timer (Winamp): stop after N minutes or at the end of the track.
        # Local playback only — the endless Pandora stream has no fixed end to
        # sleep to, so it's omitted in Pandora mode.
        if c.local_mode and hasattr(c, 'sleep_status'):
            mode, remaining, mins_set = c.sleep_status()
            sleep_menu = menu.addMenu(_('Sleep'))
            if mode == 'timer' and remaining is not None:
                sleep_menu.setTitle(_('Sleep — {}:{:02d} left').format(
                    remaining // 60, remaining % 60))
            elif mode == 'track':
                sleep_menu.setTitle(_('Sleep — end of track'))
            off = sleep_menu.addAction(_('Off'), lambda: c.set_sleep(0))
            off.setCheckable(True)
            off.setChecked(mode == 'off')
            for mins in (15, 30, 45, 60, 90):
                a = sleep_menu.addAction(_('{} minutes').format(mins),
                                         lambda *args, m=mins: c.set_sleep(m))
                a.setCheckable(True)
                a.setChecked(mode == 'timer' and mins_set == mins)
            eot = sleep_menu.addAction(_('End of Track'), c.set_sleep_end_of_track)
            eot.setCheckable(True)
            eot.setChecked(mode == 'track')
        # Time display (Elapsed/Remaining) and Visualization mode live in
        # Preferences now, so they're no longer duplicated here.
        top = menu.addAction(_('Always on Top') + '\tCtrl+A',
                             lambda: self._clutter_action('A', None))
        top.setCheckable(True)
        top.setChecked(getattr(self.window(), 'keep_above', False))
        shade = menu.addAction(_('Windowshade'), self._toggle_shade)
        shade.setCheckable(True)
        shade.setChecked(self._collapsed)
        menu.addSeparator()
        # Size (uniform scale) is a Classic-mode concern; Modern resizes by
        # widening for the album art instead.
        if getattr(self.window(), 'mode', 'modern') == 'classic':
            size = menu.addMenu(_('Size'))
            cur = getattr(self.window(), 'scale', 1)
            for label, sc in ((_('Normal (1x)'), 1.0), (_('1.5x'), 1.5), (_('Double (2x)'), 2.0)):
                a = size.addAction(label, lambda *args, v=sc: self.window().set_scale(v))
                a.setCheckable(True)
                a.setChecked(abs(cur - sc) < 0.01)
        # Skin loading is a Classic-mode concern (Modern is the enhanced view of
        # whatever skin is loaded).
        if getattr(self.window(), 'mode', 'modern') == 'classic':
            skin_menu = menu.addMenu(_('Skin'))
            skin_menu.addAction(_('Browse Skins…') + '\tAlt+S', lambda: self.window().browse_skins())
            skin_menu.addSeparator()
            skin_menu.addAction(_('Load Skin File…'), self._load_skin_file)
            skin_menu.addAction(_('Load Skin Folder…'), self._load_skin_folder)
            available = c.available_skins()
            if available:
                skin_menu.addSeparator()
                current_path = getattr(getattr(self.window(), 'skin', None), 'path', None)
                for name, path in available:
                    a = skin_menu.addAction(name, lambda *args, p=path: self.window().load_skin(p))
                    a.setCheckable(True)
                    a.setChecked(path == current_path)
        mode_menu = menu.addMenu(_('UI'))
        shell = self.window()
        cur_mode = getattr(shell, 'mode', 'modern')
        # Only offer the mode you're not already in (plus the standard view).
        if cur_mode != 'classic':
            mode_menu.addAction(_('WinAMP 2.x'), lambda: shell.set_mode('classic'))
        if cur_mode != 'modern':
            mode_menu.addAction(_('Pyrrha'), lambda: shell.set_mode('modern'))
        mode_menu.addSeparator()
        mode_menu.addAction(_('Pithos Classic'), c.show_standard_view)
        menu.addSeparator()
        menu.addAction(_('Quit'), c.quit)
        menu.exec(global_pos)

    def _scale(self):
        return getattr(self.window(), 'scale', 1)

    def _lw(self):
        shell = self.window()
        if getattr(shell, 'mode', 'modern') == 'classic':
            return W                       # pinned native in classic mode
        return max(W, int(getattr(shell, 'content_w', W)))

    def _dx(self):
        return self._lw() - W

    def display_width(self):
        return int(self._lw() * self._scale())   # stretches to the shell width

    def display_height(self):
        h = TITLEBAR[3] if self._collapsed else H   # windowshade → title bar only
        return int(h * self._scale())

    def shape_region(self):
        """Non-rectangular mask for shaped skins (region.txt), in local coords at
        the current scale; None when the skin is rectangular or the window is
        widened (region.txt only describes the native-width window)."""
        if self._lw() > W:
            return None
        sec = 'windowshade' if self._collapsed else 'normal'
        return self.skin.region(sec, self._scale())

    def _toggle_shade(self):
        shell = self.window()
        if hasattr(shell, 'toggle_shade'):
            shell.toggle_shade(self)
        else:
            self._collapsed = not self._collapsed
            self.update()

    def closeEvent(self, event):
        # Only fires when used stand-alone; inside the shell the shell quits.
        self.ctl.quit()
        super().closeEvent(event)


class SkinnedShell(QWidget):
    """A single frameless window holding the main, EQ and playlist panels
    stacked vertically. This is the one window the user sees and moves. Panels
    can collapse (windowshade) or close; the shell reflows and resizes to fit."""

    def __init__(self, controller, skin, scale=1.0):
        super().__init__(None, Qt.FramelessWindowHint)
        self.ctl = controller
        self.scale = scale      # UI scale factor (1, 1.5, 2, …)
        self.content_w = W      # shared logical content width (all panels stretch to it)
        # 'modern': album-art widen + unified resize; 'classic': faithful Winamp
        # (main/EQ pinned native, playlist independently resizable).
        self.mode = controller.get_skin_mode() if hasattr(controller, 'get_skin_mode') else 'modern'
        self.keep_above = False   # clutterbar "A" (KWin keep-above)
        self._activated_at = 0.0  # when the window last became active (click-to-focus)
        self._jump_dlg = None     # lazily-built Jump-to-File dialog (classic 'J')
        self._jump_time_dlg = None  # lazily-built Jump-to-Time dialog (Ctrl+J)
        self._vis_window = None    # lazily-built large visualizer window
        # Classic tear-off layout: each panel's *absolute* logical position inside
        # the screen-sized overlay ({'main'|'eq'|'pl': [x, y]}). Absolute (not
        # relative to main) so a torn-off panel stays put when the main window is
        # dragged; a main drag only carries the panels docked (edge-adjacent) to
        # it. Saved between runs. Free-drag state is set while a panel is dragged.
        self._pos = self._load_classic_layout()
        self._prev_region = QRegion()  # previous masked area (to clear move trails)
        self._fdrag = None        # panel currently being dragged
        self._fset = set()        # panels moving with it (the dragged panel's dock subtree)
        self._foff = {}           # each moving panel's device offset from the dragged one
        self._fraw = None         # dragged panel's un-snapped device pos (avoids snap wobble)
        self._flast = None        # last global cursor point (for delta tracking)
        self._skin_browser = None  # the Alt+S skin browser dialog (lazily built)
        # Classic mode uses the user's chosen skin; Modern always uses the
        # bundled Glare (the curated album-art experience).
        self._classic_skin = skin
        self._modern_skin = self._load_modern_skin() or skin
        self.skin = self._modern_skin if self.mode == 'modern' else self._classic_skin
        self.setWindowTitle('Pyrrha')
        # Accept keyboard focus so the shell receives key presses (used by the
        # classic "nullsoft" easter egg); no child panel takes focus.
        self.setFocusPolicy(Qt.StrongFocus)
        # Drop a .wsz / skin folder onto the window to install and apply it.
        self.setAcceptDrops(True)
        # Translucent surface so the gaps around torn-off classic panels are
        # genuinely see-through (setMask alone isn't honored visually on some
        # Wayland compositors, which leaves black there). Modern paints its own
        # black backdrop for any area a panel doesn't cover (see paintEvent).
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.main = SkinnedWindow(controller, self.skin, parent=self)
        self.eq = SkinnedEqWindow(controller, self.skin, parent=self) if self.skin.has('eqmain.bmp') else None
        self.pl = SkinnedPlaylistWindow(controller, self.skin, parent=self) if self.skin.has('pledit.bmp') else None
        self.relayout()

        # Track the monitor layout: the classic overlay spans the whole virtual
        # desktop, so a hot-plugged/removed display must regrow and re-anchor it.
        self._anchored_once = False
        app = QGuiApplication.instance()
        if app is not None:
            app.screenAdded.connect(self._on_screens_changed)
            app.screenRemoved.connect(self._on_screens_changed)
            app.primaryScreenChanged.connect(self._on_screens_changed)
        scr = self.screen() or QGuiApplication.primaryScreen()
        if scr is not None:
            scr.virtualGeometryChanged.connect(self._on_screens_changed)

    def _load_modern_skin(self):
        """The bundled Glare skin used by Modern mode (or None if unavailable)."""
        try:
            from .skin import Skin
            if hasattr(self.ctl, 'bundled_skins_dir'):
                path = os.path.join(self.ctl.bundled_skins_dir(), 'Glare')
                if os.path.isdir(path):
                    return Skin(path)
        except Exception as e:
            logging.warning('Failed to load the bundled Modern (Glare) skin: %s', e)
        return None

    def _apply_skin(self, skin):
        self.skin = skin
        for panel in (self.main, self.eq, self.pl):
            if panel is not None:
                panel.set_skin(skin)
        if self._vis_window is not None:
            self._vis_window.set_skin(skin)

    def _max_content_w(self):
        # Classic mode never widens main/EQ (they stay native, 275px).
        if self.mode == 'classic':
            return W
        # When the EQ is present, the space to its right shows the album art;
        # cap the width so that area is at most a square (its height is the EQ
        # height, H), which bounds how far the window can be dragged.
        if self.eq is not None and not self.eq._closed:
            return W + H
        return WMAX

    def set_mode(self, mode):
        """Switch between 'classic' (faithful) and 'modern' (album-art) skins."""
        if mode not in ('classic', 'modern') or mode == self.mode:
            return
        # Where the player sits on screen right now (top-left of the main panel).
        # Classic positions the player by offsetting the child inside a full-screen
        # overlay; Modern by moving the whole (small) window. Switching resizes the
        # top-level, which KWin re-anchors, so we re-place it below to keep the
        # player put: Classic returns to a (0,0) full-screen overlay (its saved
        # layout drives the panel spot), Modern lands where the player just was.
        old_player = self.main.mapToGlobal(QPoint(0, 0)) if self.main is not None else None
        self.mode = mode
        self.content_w = W
        if mode == 'modern':
            self.scale = 1.0     # Size is Classic-only; Modern runs at 1x
        # Modern always uses the bundled Glare; Classic uses the chosen skin.
        self._apply_skin(self._modern_skin if mode == 'modern' else self._classic_skin)
        if hasattr(self.ctl, 'set_skin_mode'):
            self.ctl.set_skin_mode(mode)
        self.relayout()
        self._repaint_panels()
        if mode == 'classic':
            tx, ty = self._overlay_origin()   # top-left of the whole virtual desktop
        elif old_player is not None:
            tx, ty = self._clamp_on_screen(old_player.x(), old_player.y())
        else:
            return
        # Let the resize commit before repositioning: KWin re-anchors the
        # top-left while a resize is in flight and can defeat a move issued too
        # early (the move only sticks "when not resizing"). A couple of frames is
        # imperceptible and reliably clears the pending configure.
        QTimer.singleShot(50, lambda: self._kwin_reposition(tx, ty))

    def _clamp_on_screen(self, x, y):
        """Clamp a desired top-left so the current window stays fully on-screen."""
        scr = self._avail_geom()
        if scr is None:
            return x, y
        x = max(scr.x(), min(scr.x() + scr.width() - self.width(), x))
        y = max(scr.y(), min(scr.y() + scr.height() - self.height(), y))
        return x, y

    def _kwin_reposition(self, x, y):
        """Ask KWin to move this top-level to screen (x, y). Qt move() is a no-op
        for a Wayland client, so we drive a one-shot KWin script (see
        _KWIN_MOVE_JS). KDE-only; failures elsewhere are logged and ignored."""
        try:
            from gi.repository import Gio, GLib
        except Exception:
            return
        js = _KWIN_MOVE_JS % {'pid': os.getpid(), 'x': int(x), 'y': int(y),
                              'w': int(self.width()), 'h': int(self.height())}
        path = None
        try:
            import tempfile
            fd, path = tempfile.mkstemp(suffix='.js', prefix='pyrrha-kwin-')
            with os.fdopen(fd, 'w') as f:
                f.write(js)
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            res = bus.call_sync(
                'org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting',
                'loadScript', GLib.Variant('(ss)', (path, 'pyrrha-move-%d' % int(time.monotonic() * 1000))),
                GLib.VariantType('(i)'), Gio.DBusCallFlags.NONE, 2000, None)
            sid = res.unpack()[0]
            for obj in ('/Scripting/Script%d' % sid, '/%d' % sid):
                try:
                    bus.call_sync('org.kde.KWin', obj, 'org.kde.kwin.Script',
                                  'run', None, None, Gio.DBusCallFlags.NONE, 2000, None)
                    try:
                        bus.call_sync('org.kde.KWin', obj, 'org.kde.kwin.Script',
                                      'stop', None, None, Gio.DBusCallFlags.NONE, 2000, None)
                    except GLib.Error:
                        pass
                    break
                except GLib.Error:
                    continue
        except GLib.Error as e:
            logging.debug('KWin reposition unavailable (non-KDE?): %s', e)
        except Exception as e:
            logging.debug('KWin reposition failed: %s', e)
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def relayout(self):
        # Classic mode uses free tear-off offsets (panels can be pulled apart
        # and magnetically re-snapped); Modern keeps the unified vertical stack.
        if self.mode == 'classic':
            self._classic_reflow()
            return
        self.clearMask()   # Modern is a solid rectangle (no torn-off gaps)
        panels = [p for p in (self.main, self.eq, self.pl)
                  if p is not None and not getattr(p, '_closed', False)]
        # Shared content width drives the stretchable panels (main + playlist)
        # and the album-art area beside the EQ. The shell grows to the widest
        # panel.
        self.content_w = max(W, min(self._max_content_w(), int(self.content_w)))
        shell_w = max((p.display_width() for p in panels), default=int(W * self.scale))
        y = 0
        for i, p in enumerate(panels):
            p._is_bottom = (i == len(panels) - 1)   # only the last panel has the grip
            p.setFixedSize(p.display_width(), p.display_height())
            p.move(0, y)
            p.setVisible(True)
            y += p.display_height()
        self.setFixedSize(shell_w, y)
        for p in panels:
            p.update()

    # ---------------------------------------------------- classic tear-off
    def _classic_panels(self):
        return [p for p in (self.main, self.eq, self.pl)
                if p is not None and not getattr(p, '_closed', False)]

    def _panel_role(self, p):
        return 'main' if p is self.main else 'eq' if p is self.eq else 'pl'

    def _load_classic_layout(self):
        """Load saved absolute panel positions. v2 files store each panel's
        absolute overlay position directly; legacy files stored 'main' plus
        eq/pl as offsets *relative* to main — migrate those to absolute here."""
        raw = self.ctl.get_classic_layout() if hasattr(self.ctl, 'get_classic_layout') else {}
        if not isinstance(raw, dict):
            return {}

        def _xy(v):
            if isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    return [float(v[0]), float(v[1])]
                except (TypeError, ValueError):
                    return None
            return None

        # Stored positions are scale-independent (device px at 1x); scale to the
        # current device size. Legacy files stored eq/pl as offsets relative to
        # main (also at 1x) — resolve to absolute before scaling.
        s = self.scale
        pos = {}
        main = _xy(raw.get('main'))
        if main is not None:
            pos['main'] = [main[0] * s, main[1] * s]
        legacy = raw.get('version') != 2   # old files carried no version
        for role in ('eq', 'pl'):
            v = _xy(raw.get(role))
            if v is None:
                continue
            if legacy and main is not None:
                pos[role] = [(main[0] + v[0]) * s, (main[1] + v[1]) * s]
            elif not legacy:
                pos[role] = [v[0] * s, v[1] * s]
        return pos

    def _ensure_classic_defaults(self):
        """Fill in absolute device positions for panels the user hasn't torn off
        yet: the classic main → EQ → playlist vertical stack, anchored at main's
        current spot (main defaults to the overlay origin)."""
        self._pos.setdefault('main', [0.0, 0.0])
        mx, my = self._pos['main']
        y = my + self.main.display_height()
        if self.eq is not None:
            self._pos.setdefault('eq', [mx, y])
            y += self.eq.display_height()
        if self.pl is not None:
            self._pos.setdefault('pl', [mx, y])

    def _avail_geom(self):
        scr = self.screen() or QGuiApplication.primaryScreen()
        return scr.availableGeometry() if scr is not None else None

    def _virtual_geom(self):
        """The rect the classic overlay spans: the bounding box of every
        monitor's *available* area (excludes panels/taskbars). For a single
        screen this is just its available geometry (unchanged behavior); with
        several monitors it grows to cover them all so panels can be dragged
        across displays."""
        rects = [s.availableGeometry() for s in QGuiApplication.screens()]
        if not rects:
            scr = self.screen() or QGuiApplication.primaryScreen()
            return scr.virtualGeometry() if scr is not None else None
        r = rects[0]
        for x in rects[1:]:
            r = r.united(x)
        return r

    def _overlay_origin(self):
        """Global top-left that the overlay's local (0,0) maps to — the virtual
        desktop's top-left, which is negative when a monitor sits left of / above
        the primary. Panel positions are stored relative to this."""
        vg = self._virtual_geom()
        return (vg.x(), vg.y()) if vg is not None else (0, 0)

    def _overlay_size(self):
        vg = self._virtual_geom()
        return (vg.width(), vg.height()) if vg is not None else (2000, 1500)

    def _screen_rects_local(self):
        """Each monitor's available rect in overlay-local coords (edge snapping)."""
        ox, oy = self._overlay_origin()
        return [s.availableGeometry().translated(-ox, -oy) for s in QGuiApplication.screens()]

    def _anchor_classic_overlay(self):
        """Pin the classic overlay's top-left to the virtual-desktop origin so
        panel-local coordinates line up with real screen pixels. A window sized
        to the whole virtual desktop would otherwise be placed on a single screen
        by the WM; this spans it. No-op outside classic mode or on a single screen
        (there the WM's placement already lands right, as it always has)."""
        if self.mode != 'classic' or len(QGuiApplication.screens()) <= 1:
            return
        ox, oy = self._overlay_origin()
        self.move(ox, oy)              # honored on X11; a no-op on Wayland
        self._kwin_reposition(ox, oy)  # Wayland/KDE path (uses the current size)

    def _on_screens_changed(self, *args):
        """A monitor was added/removed or the desktop was reconfigured: regrow
        the overlay to the new virtual size and re-anchor it."""
        if self.mode == 'classic':
            self.relayout()
            self._anchor_classic_overlay()

    def _classic_reflow(self):
        """Screen-sized overlay layout: size the shell to the whole screen, place
        each panel at its stored absolute device position (clamped on-screen),
        and mask the window to just the panels so the rest of the screen is
        transparent and clicks through. Positions are device px, so scaling is
        handled up front in set_scale rather than by multiplying here."""
        self._ensure_classic_defaults()
        panels = self._classic_panels()
        ow, oh = self._overlay_size()
        self.setFixedSize(ow, oh)
        region = QRegion()
        for p in panels:
            p._is_bottom = (p is self.pl)   # the playlist keeps its resize grip
            p.setFixedSize(p.display_width(), p.display_height())
            px, py = self._pos.get(self._panel_role(p), (0.0, 0.0))
            x, y = round(px), round(py)
            x = max(0, min(ow - p.width(), x))
            y = max(0, min(oh - p.height(), y))
            p.move(x, y)
            p.setVisible(True)
            # Shaped skins (region.txt) mask out corners; fall back to the full
            # rect for rectangular panels (and the playlist, always rectangular).
            shp = p.shape_region() if hasattr(p, 'shape_region') else None
            region += shp.translated(x, y) if shp is not None else QRegion(x, y, p.width(), p.height())
        # setMask clips both input *and* our own painting, so we can only clear
        # pixels inside the mask — a panel that moved leaves its old pixels behind
        # (outside the new mask) as a trail. So temporarily widen the mask to the
        # union of the previous and current panel areas, synchronously repaint it
        # (paintEvent clears the just-vacated strip to transparent), then tighten
        # the mask back to the panels for a clean input/visible region.
        vacated = QRegion(self._prev_region)
        vacated -= region                       # area a panel just left behind
        self._prev_region = QRegion(region)
        for p in panels:
            p.update()
        if vacated.isEmpty():
            # Nothing to erase (startup / in-place change): just set the mask.
            # Dropping it here would briefly unmask the whole screen and flash.
            self.setMask(region)
        else:
            # Painting is clipped to the current mask, so the vacated strip can't
            # be cleared through it. Drop the mask, synchronously repaint the old
            # + new area (paintEvent clears it to transparent), then restore the
            # panels-only mask for click-through.
            self.clearMask()
            self.repaint(region + vacated)
            self.setMask(region)

    def _apply_snap_set(self, dragged_pos):
        """Nudge the dragged panel's candidate position so any panel in the
        moving set snaps its edges to a panel *outside* the set or to a screen
        edge (magnetic docking). Chooses the closest snap per axis within
        SNAP_DIST and returns the adjusted position of the dragged panel."""
        thr = SNAP_DIST * self.scale
        externals = [o for o in self._classic_panels() if o not in self._fset]
        # Each monitor's rect in overlay-local coords: panels snap to the edges of
        # whichever display they're over, so docking works on every screen.
        screens = self._screen_rects_local()
        best_dx = best_dy = 0
        near_dx = near_dy = thr + 1
        for m in self._fset:
            off = self._foff[self._panel_role(m)]
            mv = QRect(QPoint(dragged_pos.x() + off.x(), dragged_pos.y() + off.y()), m.size())
            for o in externals:
                r = o.geometry()
                # X snap only makes sense when the panels overlap vertically.
                if not (mv.bottom() < r.top() - thr or mv.top() > r.bottom() + thr):
                    for a, e in ((mv.left(), r.left()), (mv.left(), r.right() + 1),
                                 (mv.right() + 1, r.left()), (mv.right() + 1, r.right() + 1)):
                        d = e - a
                        if abs(d) <= thr and abs(d) < near_dx:
                            near_dx, best_dx = abs(d), d
                # Y snap only when they overlap horizontally.
                if not (mv.right() < r.left() - thr or mv.left() > r.right() + thr):
                    for a, e in ((mv.top(), r.top()), (mv.top(), r.bottom() + 1),
                                 (mv.bottom() + 1, r.top()), (mv.bottom() + 1, r.bottom() + 1)):
                        d = e - a
                        if abs(d) <= thr and abs(d) < near_dy:
                            near_dy, best_dy = abs(d), d
            # Monitor edges: snap a panel edge to a display's edge only where the
            # panel overlaps that display on the other axis (so an edge on one
            # screen doesn't tug panels sitting on another).
            for sr in screens:
                if not (mv.bottom() < sr.top() - thr or mv.top() > sr.bottom() + thr):
                    for a, e in ((mv.left(), sr.left()), (mv.right() + 1, sr.right() + 1)):
                        d = e - a
                        if abs(d) <= thr and abs(d) < near_dx:
                            near_dx, best_dx = abs(d), d
                if not (mv.right() < sr.left() - thr or mv.left() > sr.right() + thr):
                    for a, e in ((mv.top(), sr.top()), (mv.bottom() + 1, sr.bottom() + 1)):
                        d = e - a
                        if abs(d) <= thr and abs(d) < near_dy:
                            near_dy, best_dy = abs(d), d
        return QPoint(dragged_pos.x() + best_dx, dragged_pos.y() + best_dy)

    def start_free_drag(self, panel, global_pos):
        """Begin dragging a panel inside the screen-sized classic overlay. The
        dragged panel carries its dock subtree (see _drag_set): dragging main
        moves the whole player, dragging a sub-window moves it and anything
        docked below it (tearing off from main). Classic mode only."""
        if self.mode != 'classic':
            return
        self._fdrag = panel
        self._flast = QPoint(global_pos)
        self._fset = self._drag_set(panel)
        base = panel.pos()
        self._foff = {self._panel_role(p): (p.pos() - base) for p in self._fset}
        self._fraw = QPoint(base)
        for p in self._fset:
            p.raise_()

    def free_drag_move(self, panel, global_pos):
        if self._fdrag is not panel:
            return
        delta = QPoint(global_pos) - self._flast
        self._flast = QPoint(global_pos)
        self._fraw += delta
        # Clamp so the whole moving set stays within the overlay, which spans the
        # entire virtual desktop (relative to the dragged panel, at self._fraw).
        ow, oh = self._overlay_size()
        offs = [self._foff[self._panel_role(p)] for p in self._fset]
        minx = min(o.x() for o in offs)
        miny = min(o.y() for o in offs)
        maxx = max(self._foff[self._panel_role(p)].x() + p.width() for p in self._fset)
        maxy = max(self._foff[self._panel_role(p)].y() + p.height() for p in self._fset)
        self._fraw.setX(max(-minx, min(ow - maxx, self._fraw.x())))
        self._fraw.setY(max(-miny, min(oh - maxy, self._fraw.y())))
        snapped = self._apply_snap_set(self._fraw)
        for p in self._fset:
            off = self._foff[self._panel_role(p)]
            self._pos[self._panel_role(p)] = [snapped.x() + off.x(), snapped.y() + off.y()]
        self._classic_reflow()

    def _are_adjacent(self, a, b):
        """True if panels a and b share a flush (touching) edge — i.e. they are
        docked. Uses current device geometry with a couple of px of slack."""
        ra, rb = a.geometry(), b.geometry()
        tol = 2
        v_overlap = min(ra.bottom(), rb.bottom()) - max(ra.top(), rb.top())
        h_overlap = min(ra.right(), rb.right()) - max(ra.left(), rb.left())
        # Side by side (shared vertical edge), overlapping vertically.
        if v_overlap > 0 and (abs(ra.right() + 1 - rb.left()) <= tol
                              or abs(rb.right() + 1 - ra.left()) <= tol):
            return True
        # Stacked (shared horizontal edge), overlapping horizontally.
        if h_overlap > 0 and (abs(ra.bottom() + 1 - rb.top()) <= tol
                              or abs(rb.bottom() + 1 - ra.top()) <= tol):
            return True
        return False

    def _docked_group(self, root):
        """The set of panels docked to ``root``, directly or transitively (a
        connected component under edge-adjacency). Undocked panels are excluded
        so a main-window drag leaves torn-off panels where they are."""
        panels = self._classic_panels()
        group = [root]
        seen = {root}
        i = 0
        while i < len(group):
            cur = group[i]
            i += 1
            for p in panels:
                if p not in seen and self._are_adjacent(cur, p):
                    seen.add(p)
                    group.append(p)
        return group

    def _drag_set(self, panel):
        """Panels that move when ``panel`` is dragged: it plus its descendants in
        the dock tree. The tree is rooted at main (so sub-windows tear off main
        instead of dragging it) or, for a cluster with no main, at its top-left-
        most panel. So dragging main moves everything docked to it, dragging a
        middle window carries what hangs below it, and dragging a leaf moves only
        itself."""
        comp = self._docked_group(panel)
        if len(comp) <= 1:
            return set(comp)
        if self.main in comp:
            root = self.main
        else:
            root = min(comp, key=lambda p: (self._pos[self._panel_role(p)][1],
                                            self._pos[self._panel_role(p)][0]))
        parent = {root: None}
        queue = [root]
        while queue:
            cur = queue.pop(0)
            for p in comp:
                if p not in parent and self._are_adjacent(cur, p):
                    parent[p] = cur
                    queue.append(p)
        children = {}
        for node, par in parent.items():
            if par is not None:
                children.setdefault(par, []).append(node)
        result, stack = set(), [panel]
        while stack:
            cur = stack.pop()
            if cur in result:
                continue
            result.add(cur)
            stack.extend(children.get(cur, ()))
        return result

    def toggle_shade(self, panel):
        """Windowshade a panel (collapse to / expand from its title bar). In
        classic mode a height change would leave a gap or overlap under the
        panel, so shift the panels docked below it by the delta to keep them
        docked. Modern's relayout re-stacks vertically on its own."""
        if self.mode == 'classic':
            old_h = panel.display_height()
            panel._collapsed = not panel._collapsed
            self._shift_docked_below(panel, panel.display_height() - old_h)
        else:
            panel._collapsed = not panel._collapsed
        self.relayout()
        panel.update()

    def resize_panel_height(self, panel, new_h):
        """Set a resizable panel's logical height (the playlist's corner grip),
        shifting panels docked below it so they track the moving bottom edge in
        classic mode."""
        if getattr(panel, '_height', None) == new_h:
            return
        if self.mode == 'classic':
            old_dh = panel.display_height()
            panel._height = new_h
            self._shift_docked_below(panel, panel.display_height() - old_dh)
        else:
            panel._height = new_h
        self.relayout()

    def _shift_docked_below(self, panel, dy):
        """Move panels docked below ``panel`` (and anything docked to them) by
        ``dy`` device px, so they stay flush when the panel's height changes.
        Reads the pre-relayout geometry, so call before relayout()."""
        if not dy:
            return
        rp = panel.geometry()   # still the old rect (not yet relaid out)
        tol = 2
        seeds = []
        for o in self._classic_panels():
            if o is panel:
                continue
            r = o.geometry()
            h_overlap = min(r.right(), rp.right()) - max(r.left(), rp.left())
            if h_overlap > 0 and abs(r.top() - (rp.bottom() + 1)) <= tol:
                seeds.append(o)
        move, stack = set(), list(seeds)
        while stack:
            cur = stack.pop()
            if cur in move:
                continue
            move.add(cur)
            for o in self._classic_panels():
                if o is not panel and o not in move and self._are_adjacent(cur, o):
                    stack.append(o)
        for p in move:
            self._pos[self._panel_role(p)][1] += dy

    def end_free_drag(self):
        self._fdrag = self._fraw = self._flast = None
        self._fset = set()
        self._foff = {}
        if hasattr(self.ctl, 'set_classic_layout'):
            # Persist scale-independent (1x device) positions so the layout
            # reloads correctly regardless of the scale in effect when saved.
            s = self.scale or 1.0
            data = {role: [xy[0] / s, xy[1] / s] for role, xy in self._pos.items()}
            data['version'] = 2   # absolute positions (see _load_classic_layout)
            self.ctl.set_classic_layout(data)

    def paintEvent(self, event):
        # The shell surface is translucent; Modern wants a black backdrop behind
        # any area a panel doesn't cover. Classic is the screen-sized overlay:
        # actively clear to *fully* transparent (Source mode zeroes the alpha —
        # a normal transparent fill would composite to nothing and leave the old
        # pixels), erasing the trails left when panels move.
        p = QPainter(self)
        if self.mode == 'classic':
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(event.rect(), Qt.transparent)
        else:
            p.fillRect(self.rect(), Qt.black)
        p.end()

    def set_content_width(self, logical_w):
        self.content_w = max(W, min(self._max_content_w(), int(logical_w)))
        self.relayout()

    def load_skin(self, path):
        """Load a skin for Classic mode (Modern always uses the bundled Glare)."""
        from .skin import Skin
        try:
            skin = Skin(path)
        except Exception as e:
            logging.warning('Failed to load skin %s: %s', path, e)
            return False
        if not skin.has('main.bmp'):
            logging.warning('Not a valid Winamp skin (no main.bmp): %s', path)
            return False
        self._classic_skin = skin
        if self.mode == 'classic':
            self._apply_skin(skin)
            self.relayout()
        self.ctl.set_last_skin(path)
        return True

    def wheelEvent(self, event):
        # Scroll anywhere over the window to change volume (classic Winamp).
        notches = event.angleDelta().y() / 120.0
        if notches and self.main is not None:
            self.main.change_volume(notches / 27.0)   # one 28-step slider notch
            event.accept()

    @staticmethod
    def _skin_paths(mime):
        """Local skin paths (.wsz/.zip files or folders with a main.bmp) from a
        drag's mime data."""
        out = []
        if not mime.hasUrls():
            return out
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            p = url.toLocalFile()
            if p.lower().endswith(('.wsz', '.zip')):
                out.append(p)
            elif os.path.isdir(p):
                try:
                    if any(f.lower() == 'main.bmp' for f in os.listdir(p)):
                        out.append(p)
                except OSError:
                    pass
        return out

    def install_and_apply_skin(self, path):
        """Install a dropped/loaded skin into the library and show it (switching
        to WinAMP 2.x so it's visible). Refreshes the browser if it's open."""
        installed = self.ctl.install_skin(path) if hasattr(self.ctl, 'install_skin') else path
        if self.mode != 'classic':
            self.set_mode('classic')
        self.load_skin(installed)
        if getattr(self, '_skin_browser', None) is not None:
            self._skin_browser.reload()

    @staticmethod
    def _audio_paths(mime):
        """Local audio files, playlists, or folders from a drag's mime data
        (classic Winamp drop-to-add). Directories are handed to the controller,
        which scans them for playable files."""
        from ..local import AUDIO_EXTENSIONS, PLAYLIST_EXTENSIONS
        out = []
        if not mime.hasUrls():
            return out
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            p = url.toLocalFile()
            low = p.lower()
            if (os.path.isdir(p) or low.endswith(AUDIO_EXTENSIONS)
                    or low.endswith(PLAYLIST_EXTENSIONS)):
                out.append(p)
        return out

    def dragEnterEvent(self, event):
        if self._skin_paths(event.mimeData()) or self._audio_paths(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        # Skins take priority (a skin folder is also a directory); audio next.
        skins = self._skin_paths(event.mimeData())
        if skins:
            self.install_and_apply_skin(skins[0])
            event.acceptProposedAction()
            return
        audio = self._audio_paths(event.mimeData())
        if audio and hasattr(self.ctl, 'add_local_files'):
            self.ctl.add_local_files(audio)
            event.acceptProposedAction()

    def browse_skins(self):
        """Open the Winamp-style skin browser (Alt+S); reuse the one instance."""
        if getattr(self, '_skin_browser', None) is None:
            from ..dialogs.skinbrowser import SkinBrowser
            self._skin_browser = SkinBrowser(self, self.ctl, self)
        else:
            self._skin_browser.reload()
        self._skin_browser.show()
        self._skin_browser.raise_()
        self._skin_browser.activateWindow()

    def keyPressEvent(self, event):
        # Alt+S opens the skin browser (classic Winamp shortcut).
        if event.key() == Qt.Key_S and (event.modifiers() & Qt.AltModifier):
            self.browse_skins()
            event.accept()
            return
        # Route navigation keys to the playlist (the shell keeps focus, so this
        # avoids child focus juggling while letting arrows/Enter/Delete work).
        if (self.pl is not None and not getattr(self.pl, '_closed', False)
                and self.pl.handle_key(event)):
            event.accept()
            return
        # Feed keys to the classic "nullsoft" easter egg matcher.
        if self.main is not None and self.main.egg_key(event):
            event.accept()
            return
        # Classic Winamp keyboard map (transport, seek, volume, jump-to-file).
        if self._dispatch_shortcut(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def _dispatch_shortcut(self, event):
        """The classic Winamp keyboard map. Returns True if the key was handled.
        Runs after playlist navigation (so arrows still drive the list when it's
        open) and after the easter-egg matcher (so 's'/'l' feed a live egg
        sequence before acting)."""
        c = self.ctl
        key = event.key()
        mods = event.modifiers()
        if mods & Qt.ControlModifier:
            if key == Qt.Key_D:            # Ctrl+D: double-size toggle (Classic)
                self.toggle_scale()
                return True
            if key == Qt.Key_P:            # Ctrl+P: preferences
                c.show_preferences()
                return True
            if key == Qt.Key_A:            # Ctrl+A: always-on-top
                if self.main is not None:
                    self.main._clutter_action('A', None)
                return True
            if key == Qt.Key_J:            # Ctrl+J: jump to time
                self.open_jump_to_time()
                return True
            return False                   # leave other Ctrl combos alone
        if mods & (Qt.AltModifier | Qt.MetaModifier):
            if (mods & Qt.AltModifier) and key == Qt.Key_3 and hasattr(c, 'info_song'):
                c.info_song()              # Alt+3: file / song info
                return True
            return False                   # Alt+S etc. handled above
        # Seek (Left/Right) and volume (Up/Down). Arrows reach here only when the
        # playlist did not already consume them for row navigation.
        if key == Qt.Key_Left:
            self._key_seek(-SEEK_STEP_NS)
            return True
        if key == Qt.Key_Right:
            self._key_seek(SEEK_STEP_NS)
            return True
        if key == Qt.Key_Up:
            if self.main is not None:
                self.main.change_volume(KEY_VOL_STEP)
            return True
        if key == Qt.Key_Down:
            if self.main is not None:
                self.main.change_volume(-KEY_VOL_STEP)
            return True
        text = event.text().lower()
        if len(text) != 1:
            return False
        transport = {'z': c.prev_song, 'x': c.user_play, 'c': c.user_playpause,
                     'v': c.stop, 'b': c.next_song, 'l': c.open_local_files}
        if text in transport:
            transport[text]()
            return True
        if text == 's':                    # shuffle (local play order)
            c.toggle_shuffle()
            self._repaint_panels()
            return True
        if text == 'r':                    # repeat (local play order)
            c.toggle_repeat()
            self._repaint_panels()
            return True
        if text == 'j':                    # jump to file
            self.open_jump_to_file()
            return True
        return False

    def _key_seek(self, delta_ns):
        """Seek the current track by a relative amount (no-op for Pandora, which
        is not seekable)."""
        c = self.ctl
        if not c.seekable():
            return
        pos = c.query_position() or 0
        c.seek(pos + delta_ns)
        if self.main is not None:
            self.main.update()

    def open_jump_to_file(self):
        """Open (or re-focus) the classic Winamp 'Jump to File' quick-search."""
        from .jumpto import JumpToFileDialog
        if self._jump_dlg is None:
            self._jump_dlg = JumpToFileDialog(self.ctl, self)
        self._jump_dlg.popup()

    def open_jump_to_time(self):
        """Open the classic Winamp 'Jump to Time' box (Ctrl+J). Seeking is a
        no-op on Pandora, which is not seekable."""
        from .jumpto import JumpToTimeDialog
        if self._jump_time_dlg is None:
            self._jump_time_dlg = JumpToTimeDialog(self.ctl, self)
        self._jump_time_dlg.popup()

    def open_vis_window(self):
        """Open (or re-focus) the large visualizer window, sharing the skin
        palette and the controller's live spectrum."""
        from .viswindow import VisWindow
        if self._vis_window is None:
            self._vis_window = VisWindow(self.ctl, self.skin, self)
        self._vis_window.show()
        self._vis_window.raise_()
        self._vis_window.activateWindow()

    def toggle_width(self):
        """Widen the player to reveal the album art beside the EQ (a square),
        or collapse back to native width. Modern mode, EQ open."""
        if self.mode == 'classic' or self.eq is None or self.eq._closed:
            return
        self.set_content_width(W if self.content_w > W else W + H)

    def set_scale(self, scale):
        scale = max(1.0, min(4.0, scale))
        if self.mode == 'classic' and abs(scale - self.scale) > 1e-6:
            self._rescale_positions(self.scale, scale)
        self.scale = scale
        self.relayout()
        for panel in (self.main, self.eq, self.pl):
            if panel is not None:
                panel.update()

    def _rescale_positions(self, s0, s1):
        """Grow/shrink the classic layout in place when the UI scale changes.
        Each docked cluster is scaled about its anchor so it grows from that
        corner and stays docked: the main cluster anchors on the main window;
        any torn-off cluster on its own top-left-most panel. Call before
        updating self.scale (adjacency is read from the current geometry)."""
        ratio = s1 / s0
        visited = set()
        for start in self._classic_panels():
            if start in visited:
                continue
            comp = self._docked_group(start)
            visited.update(comp)
            if self.main in comp:
                anchor = self.main
            else:
                anchor = min(comp, key=lambda p: tuple(self._pos[self._panel_role(p)][::-1]))
            ax, ay = self._pos[self._panel_role(anchor)]
            for p in comp:
                role = self._panel_role(p)
                px, py = self._pos[role]
                self._pos[role] = [ax + (px - ax) * ratio, ay + (py - ay) * ratio]

    def toggle_scale(self):
        self.set_scale(2 if self.scale == 1 else 1)

    def toggle_panel(self, panel):
        """Open a closed panel or close an open one (the main window's EQ/PL
        buttons and each panel's own close button share this state)."""
        if panel is None:
            return
        panel._closed = not panel._closed
        if panel._closed:
            panel.hide()
            if panel is self.pl:
                self.content_w = W   # collapse the player back to default width
        self.relayout()

    def _repaint_panels(self):
        for panel in (self.main, self.eq, self.pl):
            if panel is not None:
                panel.update()

    def showEvent(self, event):
        # Restoring from the tray (hidden -> shown) must repaint the panels, or
        # the album-art area can come back from a stale backing store.
        super().showEvent(event)
        # Anchor the classic overlay to the virtual-desktop origin the first time
        # it appears (single-monitor setups land there anyway; multi-monitor ones
        # need it). Deferred so the size is committed before we move it.
        if self.mode == 'classic' and not self._anchored_once:
            self._anchored_once = True
            QTimer.singleShot(0, self._anchor_classic_overlay)
        self._repaint_panels()

    def changeEvent(self, event):
        # Same for un-minimizing (a window-state change, not a show event).
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and not self.isMinimized():
            self._repaint_panels()
        elif event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self._activated_at = time.monotonic()   # for click-to-focus

    def is_focus_click(self):
        """True if a click arriving now is the one that just activated the window
        (so controls shouldn't fire — the click only raises/focuses)."""
        if time.monotonic() - self._activated_at < 0.25:
            self._activated_at = 0.0
            return True
        return False

    def closeEvent(self, event):
        self.ctl.quit()
        super().closeEvent(event)
