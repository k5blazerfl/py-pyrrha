# -*- coding: utf-8 -*-
# Pyrrha - a Qt port of Pithos.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3.

"""The classic Winamp equalizer window (prototype), wired to Pyrrha's real
10-band GStreamer equalizer element.

Renders EQMAIN.BMP: the 10 band sliders (+ a preamp slider), an ON toggle and
the response-curve graph. Dragging a band slider drives
``controller.equalizer`` (the ``equalizer-10bands`` element) live; the ON toggle
flattens/restores it. Sliders are mapped ±12 dB (middle = 0 dB), within the
element's -24..+12 range. Slider-column and button offsets follow the classic
EQMAIN spec and are easy to tune against a real skin.
"""

import logging
import os

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QFileDialog, QMenu, QWidget

W, H = 275, 116
BANDS = 10

TITLE_H = 14
THUMB_W, THUMB_H = 11, 11
# Thumb-top range so the 11px thumb centers on the skin's +12/0/-12 dB track
# guides (base 2.91 has them at y=39/68/98).
SLIDER_TOP = 34
SLIDER_TRAVEL = 59          # vertical thumb travel (px)
PREAMP_X = 21
BAND_X0, BAND_DX = 78, 18   # band columns: 78, 96, ... 240 (spread out when wider)
GRAPH = QRect(86, 17, 113, 19)
ON_BTN = QRect(14, 18, 25, 12)
AUTO_BTN = QRect(39, 18, 34, 12)       # to the right of ON; sprites at y=119
PRESETS_BTN = QRect(217, 18, 44, 12)   # opens the preset menu; sprite at (224,164)
MIN_BTN = QRect(254, 3, 9, 9)     # windowshade: collapse to the title bar
CLOSE_BTN = QRect(264, 3, 9, 9)   # close (hide) the panel

# EQ_EX.BMP: the equalizer's dedicated windowshade titlebar (distinct from the
# full-window titlebar in eqmain.bmp — decorative bars + groove, no "EQUALIZER"
# text). Active row at y=0, inactive at y=15; each 275x14. Skins may omit it.
EQEX = 'eq_ex.bmp'
EQEX_TB_ACTIVE, EQEX_TB_INACTIVE = 0, 15

DB_RANGE = 12.0             # slider maps +/- this many dB (middle = 0)
DB_MIN, DB_MAX = -24.0, 12.0  # element's actual limits

# Slider well: the colored VU bar behind each thumb. 28 frames (2 rows of 14)
# in eqmain.bmp; magenta (255,0,255) is the transparency key. Skins without
# wells (e.g. Glare) leave this region magenta, so nothing is drawn.
WELL_W, WELL_H = 14, 63
WELL_TOP = 34
WELL_FRAMES = 28
MAGENTA = 0xFF00FF

# Classic 10-band EQ presets (dB per band, low → high frequency).
PRESETS = [
    ('Flat',              [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ('Rock',              [8, 5, -4, -7, -3, 3, 7, 10, 11, 11]),
    ('Pop',               [-1, 4, 7, 8, 5, 0, -2, -2, -1, -1]),
    ('Jazz',              [4, 3, 1, 2, -2, -2, 0, 1, 3, 4]),
    ('Classical',         [0, 0, 0, 0, 0, 0, -6, -6, -6, -8]),
    ('Dance',             [7, 5, 2, 0, 0, -4, -6, -6, 0, 0]),
    ('Electronic',        [5, 4, 1, 0, -2, 2, 1, 1, 5, 6]),
    ('Hip-Hop',           [6, 5, 2, 3, -1, -1, 1, -1, 2, 3]),
    ('Vocal',             [-2, -3, -3, 1, 4, 4, 3, 1, 0, -2]),
    ('Bass Boost',        [8, 7, 6, 4, 1, 0, 0, 0, 0, 0]),
    ('Treble Boost',      [0, 0, 0, 0, 0, 1, 3, 5, 7, 8]),
    ('Loudness',          [7, 5, 0, 0, -3, 0, -1, -6, 6, 1]),
]

PRESETS_BY_NAME = {name: values for name, values in PRESETS}

# The "AUTO" button maps free-text metadata (Pandora gives us no genre field) to
# one of the presets above. Pandora station names are the strongest signal —
# genre stations are literally named for the genre — with the song's
# title/album/artist as a weaker fallback. Substrings are matched in order, so
# list the more specific genres first ("classical"/"hip-hop" before the broad
# "rock"/"pop", which would otherwise swallow "classic rock" etc.).
GENRE_KEYWORDS = [
    ('classical', 'Classical'), ('symphon', 'Classical'), ('orchestr', 'Classical'),
    ('concerto', 'Classical'), ('baroque', 'Classical'), ('opera', 'Classical'),
    ('chamber', 'Classical'),
    ('hip-hop', 'Hip-Hop'), ('hip hop', 'Hip-Hop'), ('hiphop', 'Hip-Hop'),
    ('rap', 'Hip-Hop'), ('trap', 'Hip-Hop'), ('r&b', 'Hip-Hop'), ('rnb', 'Hip-Hop'),
    ('urban', 'Hip-Hop'),
    ('electronic', 'Electronic'), ('synth', 'Electronic'), ('ambient', 'Electronic'),
    ('downtempo', 'Electronic'), ('chill', 'Electronic'), ('idm', 'Electronic'),
    ('dance', 'Dance'), ('edm', 'Dance'), ('house', 'Dance'), ('techno', 'Dance'),
    ('trance', 'Dance'), ('disco', 'Dance'), ('club', 'Dance'),
    ('jazz', 'Jazz'), ('swing', 'Jazz'), ('bebop', 'Jazz'), ('blues', 'Jazz'),
    ('dubstep', 'Bass Boost'), ('reggae', 'Bass Boost'), ('bass', 'Bass Boost'),
    ('acoustic', 'Vocal'), ('singer-songwriter', 'Vocal'), ('folk', 'Vocal'),
    ('vocal', 'Vocal'), ('a cappella', 'Vocal'), ('acappella', 'Vocal'),
    ('country', 'Vocal'),
    ('metal', 'Rock'), ('rock', 'Rock'), ('punk', 'Rock'), ('grunge', 'Rock'),
    ('alternative', 'Rock'), ('indie', 'Rock'),
    ('pop', 'Pop'), ('hits', 'Pop'), ('top 40', 'Pop'), ('charts', 'Pop'),
]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _match_genre(text):
    """Return the preset name a metadata string implies, or None."""
    t = text.lower()
    for kw, name in GENRE_KEYWORDS:
        if kw in t:
            return name
    return None


# --- Winamp EQ preset files (.eqf / winamp.q1) -----------------------------
# Layout: a 31-byte signature, then per preset a 257-byte null-padded name, 10
# band bytes (low→high) and 1 preamp byte. Each value is 0..63, where 0 = +12 dB
# (slider top), 31/32 ≈ 0 dB and 63 = -12 dB. Bands are ordered low→high, the
# same as our _bands / the GStreamer equalizer, so indices map directly.
EQF_SIGNATURE = b'Winamp EQ library file v1.1\x1a!--'
EQF_NAME_LEN = 257


def _eqf_val_to_db(v):
    return (31.5 - v) * (24.0 / 63.0)


def _eqf_db_to_val(db):
    return max(0, min(63, int(round(31.5 - db * (63.0 / 24.0)))))


def parse_eqf(data):
    """Parse .eqf/.q1 bytes into a list of (name, bands[10] dB, preamp dB)."""
    if not data.startswith(EQF_SIGNATURE):
        raise ValueError('not a Winamp EQ library file')
    out = []
    off = len(EQF_SIGNATURE)
    rec = EQF_NAME_LEN + BANDS + 1
    while off + rec <= len(data):
        raw_name = data[off:off + EQF_NAME_LEN].split(b'\x00', 1)[0]
        name = raw_name.decode('latin-1', 'replace').strip() or 'Preset'
        vals = data[off + EQF_NAME_LEN:off + rec]
        bands = [_eqf_val_to_db(v) for v in vals[:BANDS]]
        out.append((name, bands, _eqf_val_to_db(vals[BANDS])))
        off += rec
    return out


def build_eqf(presets):
    """Serialize [(name, bands[10] dB, preamp dB)] into .eqf bytes."""
    buf = bytearray(EQF_SIGNATURE)
    for name, bands, preamp in presets:
        nm = name.encode('latin-1', 'replace')[:EQF_NAME_LEN - 1]
        buf += nm + b'\x00' * (EQF_NAME_LEN - len(nm))
        for i in range(BANDS):
            buf.append(_eqf_db_to_val(bands[i] if i < len(bands) else 0.0))
        buf.append(_eqf_db_to_val(preamp))
    return bytes(buf)


class SkinnedEqWindow(QWidget):
    def __init__(self, controller, skin, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint if parent is None else Qt.Widget)
        self.ctl = controller
        self.skin = skin
        self.eq = controller.equalizer
        self.setFixedSize(W, H)
        self.setWindowTitle('Pyrrha EQ')

        self._on = True
        self._auto = bool(controller.settings['eq-auto'])
        self._preamp = 0.0
        self._bands = [float(self.eq.get_property('band%d' % i)) for i in range(BANDS)]
        self._drag = None   # index of slider being dragged (-1 = preamp)
        self._collapsed = False
        self._closed = False
        self._cursor_name = False    # region cursor currently set (False = unknown)
        self._well_cache = {}       # frame index -> magenta-keyed QImage
        self._eqex_present = None    # does this skin ship the windowshade titlebar?
        self._wells_present = None   # does this skin have slider wells?
        self._well_offset = None     # x offset to center the well under the thumb
        self._curve_colors_cache = None   # per-row EQ response-curve colors

        # Repaint the album art when the song or its artwork changes. The model's
        # dataChanged is the reliable trigger: art_callback always updates the row
        # (metadata_changed is skipped when the art cache write fails).
        controller.song_changed.connect(lambda *_: self.update())
        # When AUTO is on, re-pick the genre preset as songs (and their
        # metadata) arrive; a no-op otherwise.
        controller.song_changed.connect(lambda *_: self._apply_auto_genre())
        controller.metadata_changed.connect(lambda *_: self._apply_auto_genre())
        controller.metadata_changed.connect(lambda *_: self.update())
        model = getattr(controller, 'songs_model', None)
        if model is not None and hasattr(model, 'dataChanged'):
            model.dataChanged.connect(self._on_rows_changed)
        # Restore the remembered EQ when the active profile changes: per-station
        # under Pandora, and the shared Local/Radio curves on a mode switch.
        controller.station_changed_sig.connect(lambda *_: self._restore_eq_profile())
        controller.source_changed_sig.connect(lambda *_: self._restore_eq_profile())
        # And restore the current mode's curve at startup (don't launch flat).
        self._restore_eq_profile()

    def set_skin(self, skin):
        self.skin = skin
        self._cursor_name = False
        self.unsetCursor()
        self._well_cache = {}
        self._eqex_present = None
        self._wells_present = None
        self._well_offset = None
        self._curve_colors_cache = None
        self.update()

    def _cursor_for(self, pos):
        if self._collapsed or pos.y() < TITLE_H:
            return 'eqtitle.cur'
        if self._slider_at(pos) is not None:
            return 'eqslid.cur'
        return None

    def _update_cursor(self, pos):
        if not self.skin.has_cursors:
            return
        name = self._cursor_for(pos)
        if name == self._cursor_name:
            return
        self._cursor_name = name
        cur = self.skin.cursor(name) if name else None
        self.setCursor(cur) if cur is not None else self.unsetCursor()

    def _has_eqex(self):
        """Does this skin ship eq_ex.bmp (the windowshade titlebar)? Cached."""
        if self._eqex_present is None:
            img = self.skin.image(EQEX)
            self._eqex_present = img is not None and not img.isNull()
        return self._eqex_present

    # ----------------------------------------------------------- slider wells
    def _has_wells(self):
        """Does this skin ship real slider-well graphics? Winamp draws the well
        (the slider track / VU bar) under each thumb. Skins that omit it key the
        well region out with magenta (Winamp's "empty" convention, e.g. Glare);
        a skin with real wells fills the region with solid graphics. Detecting
        this by the magenta fraction handles grayscale wells (e.g. the Classified
        skin) too — a color-saturation test misread those as empty, so wells were
        skipped and the thumb's opaque corners boxed out on the light track."""
        if self._wells_present is None:
            img = self.skin.image('eqmain.bmp')
            present = False
            if not img.isNull() and img.height() >= WELL_TOP + WELL_H:
                mag = tot = 0
                for x in range(8, 240, 4):
                    for y in range(164, 227, 4):     # the (row-0) well strip
                        tot += 1
                        if (img.pixel(x, y) & 0xFFFFFF) == MAGENTA:
                            mag += 1
                present = tot > 0 and mag < tot * 0.1   # mostly solid ⇒ real wells
            self._wells_present = present
        return self._wells_present

    def _well_x_offset(self):
        """Where to draw a well (relative to the slider column) so its groove
        sits under the thumb. Skins place the slider track at different x within
        the well cell, so derive it from the sprite rather than assuming it's
        centered: locate the groove as the horizontal centre-of-mass of the
        columns that differ most from the well's edge columns, and offset so it
        lands under the thumb centre. Verified across a skin corpus to centre
        every skin (vs a fixed offset that left some off by a few px). Cached."""
        if self._well_offset is None:
            off = -2   # fallback if the well sprite is unreadable
            w = self.skin.sprite('eqmain.bmp', 13, 164, WELL_W, WELL_H)
            if not w.isNull():
                rows = list(range(10, WELL_H - 10, 3))
                cv = [sum((lambda c: (c.red() + c.green() + c.blue()) // 3)(w.pixelColor(x, y))
                          for y in rows) / len(rows) for x in range(WELL_W)]
                edge = (cv[0] + cv[1] + cv[-1] + cv[-2]) / 4
                wt = [abs(v - edge) for v in cv]
                total = sum(wt)
                if total >= 1:
                    groove = sum(x * wt[x] for x in range(WELL_W)) / total
                    off = round(THUMB_W // 2 - groove)
            self._well_offset = off
        return self._well_offset

    def _well_sprite(self, frame):
        img = self._well_cache.get(frame)
        if img is None:
            col, row = frame % 14, frame // 14
            img = self.skin.sprite('eqmain.bmp', 13 + col * 15, 164 + row * 65,
                                   WELL_W, WELL_H).convertToFormat(QImage.Format_ARGB32)
            for y in range(img.height()):        # key out magenta transparency
                for x in range(img.width()):
                    if (img.pixel(x, y) & 0xFFFFFF) == MAGENTA:
                        img.setPixelColor(x, y, QColor(0, 0, 0, 0))
            self._well_cache[frame] = img
        return img

    def _band_frame(self, db):
        f = int(round((db + DB_RANGE) / (2 * DB_RANGE) * (WELL_FRAMES - 1)))
        return max(0, min(WELL_FRAMES - 1, f))

    def _scale(self):
        return getattr(self.window(), 'scale', 1)

    def _lw(self):
        shell = self.window()
        if getattr(shell, 'mode', 'modern') == 'classic':
            return W                       # native in classic mode (no album art)
        return max(W, int(getattr(shell, 'content_w', W)))

    def display_width(self):
        # The EQ face itself can't stretch (its sliders, graph box, labels and
        # preset bar are baked in), so it stays native at the left; the panel
        # spans the full width and shows the album art in the area to its right.
        return int(self._lw() * self._scale())

    def display_height(self):
        return int((TITLE_H if self._collapsed else H) * self._scale())

    def shape_region(self):
        """Non-rectangular mask for shaped skins (region.txt), in local coords at
        the current scale; None when rectangular or widened for the album art."""
        if self._lw() > W:
            return None
        sec = 'equalizerws' if self._collapsed else 'equalizer'
        return self.skin.region(sec, self._scale())

    def _toggle_collapse(self):
        shell = self.window()
        if hasattr(shell, 'toggle_shade'):
            shell.toggle_shade(self)
        else:
            self._collapsed = not self._collapsed
            self.window().relayout()

    def _close_panel(self):
        self._closed = True
        self.hide()
        self.window().relayout()

    # ----------------------------------------------------------- geometry
    def _col_x(self, i):
        # The sliders, graph box and frequency labels are baked into the skin
        # at fixed positions, so they stay native; only the panel widens.
        return PREAMP_X if i == -1 else BAND_X0 + i * BAND_DX

    def _value_to_y(self, db):
        f = _clamp((DB_RANGE - db) / (2 * DB_RANGE), 0.0, 1.0)
        return SLIDER_TOP + int(round(f * SLIDER_TRAVEL))

    def _y_to_value(self, y):
        f = _clamp((y - SLIDER_TOP) / SLIDER_TRAVEL, 0.0, 1.0)
        return DB_RANGE - f * (2 * DB_RANGE)

    def _slider_at(self, pos):
        for i in range(-1, BANDS):
            x = self._col_x(i)
            if QRect(x, SLIDER_TOP, THUMB_W, SLIDER_TRAVEL + THUMB_H).contains(pos):
                return i
        return None

    # -------------------------------------------------------------- audio
    def _apply(self):
        # The equalizer-10bands element has no preamp, so fold the preamp into
        # every band as a broadband gain (clamped to the element's range).
        for i in range(BANDS):
            val = _clamp(self._bands[i] + self._preamp, DB_MIN, DB_MAX) if self._on else 0.0
            self.eq.set_property('band%d' % i, val)

    # ------------------------------------------------------------ presets
    def _show_presets_menu(self, global_pos):
        menu = QMenu(self)
        for name, values in PRESETS:
            menu.addAction(name, lambda *a, v=values: self._apply_preset(v))
        menu.addSeparator()
        menu.addAction(_('Load Preset File…'), lambda: self._load_preset_file(global_pos))
        menu.addAction(_('Save Preset File…'), self._save_preset_file)
        menu.addAction(_('Reset Preamp'), self._reset_preamp)
        menu.exec(global_pos)

    def _load_preset_file(self, anchor_pos):
        """Import a Winamp .eqf/.q1 preset file. Applies the sole preset, or pops
        a chooser when the file is a library of several."""
        path, _sel = QFileDialog.getOpenFileName(
            self, _('Load Winamp EQ Preset'), '',
            _('Winamp EQ presets (*.eqf *.q1 *.q2);;All files (*)'))
        if not path:
            return
        try:
            with open(path, 'rb') as f:
                presets = parse_eqf(f.read())
        except (OSError, ValueError) as e:
            logging.warning('Failed to load EQ preset %s: %s', path, e)
            return
        if not presets:
            return
        if len(presets) == 1:
            _n, bands, preamp = presets[0]
            self._apply_preset(bands, preamp)
            return
        chooser = QMenu(self)
        for name, bands, preamp in presets:
            chooser.addAction(name, lambda *a, b=bands, p=preamp: self._apply_preset(b, p))
        chooser.exec(anchor_pos)

    def _save_preset_file(self):
        """Export the current bands + preamp as a single-preset Winamp .eqf."""
        path, _sel = QFileDialog.getSaveFileName(
            self, _('Save Winamp EQ Preset'), 'preset.eqf',
            _('Winamp EQ presets (*.eqf);;All files (*)'))
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += '.eqf'
        name = os.path.splitext(os.path.basename(path))[0] or 'Pyrrha'
        try:
            with open(path, 'wb') as f:
                f.write(build_eqf([(name, self._bands, self._preamp)]))
        except OSError as e:
            logging.warning('Failed to save EQ preset %s: %s', path, e)

    def _reset_preamp(self):
        self._preamp = 0.0
        if self._on:
            self._apply()
        self._save_eq_profile()
        self.update()

    # ------------------------------------------------------ EQ persistence
    def _save_eq_profile(self):
        # AUTO owns the curve while on; its derived genre presets (and any
        # transient hand-tweaks over them) must not overwrite the profile's own
        # remembered curve.
        if self._auto:
            return
        self.ctl.set_eq_profile(self._bands, self._preamp, self._on)

    # -------------------------------------------------------- AUTO genre
    def _pick_genre_preset(self):
        """Preset band list implied by the current station/song metadata, or
        None when nothing classifies. The station name is Pandora's clearest
        genre cue; the track's own text is a weaker fallback."""
        station = getattr(self.ctl, 'current_station', None)
        name = _match_genre(getattr(station, 'name', '') or '')
        if name is None:
            song = getattr(self.ctl, 'current_song', None)
            if song is not None:
                corpus = ' '.join(str(getattr(song, a, '') or '')
                                  for a in ('title', 'songName', 'album', 'artist'))
                name = _match_genre(corpus)
        return PRESETS_BY_NAME.get(name) if name else None

    def _apply_auto_genre(self):
        """Override the curve with the auto-selected genre preset (Flat when the
        metadata doesn't classify). No-op unless AUTO is engaged."""
        if not self._auto:
            return
        values = self._pick_genre_preset() or PRESETS_BY_NAME['Flat']
        bands = [_clamp(float(v), DB_MIN, DB_MAX) for v in values]
        self._bands = (bands + [0.0] * BANDS)[:BANDS]
        self._preamp = 0.0
        self._on = True
        self._apply()
        self.update()

    def _on_rows_changed(self, top_left, bottom_right, *roles):
        # Repaint if the current song's row changed (e.g. its art just arrived).
        cur = getattr(self.ctl, 'current_song_index', None)
        if cur is not None and top_left.row() <= cur <= bottom_right.row():
            self.update()

    def _restore_eq_profile(self):
        """Load the active profile's remembered EQ — per-station under Pandora, a
        shared curve for each of Local and Radio. AUTO overrides with a genre pick
        while engaged; a profile with nothing saved starts flat."""
        if self._auto:                   # AUTO drives the curve from metadata
            self._apply_auto_genre()
            return
        saved = self.ctl.get_eq_profile()
        if saved:
            bands = [_clamp(float(v), DB_MIN, DB_MAX) for v in saved.get('bands', [])]
            self._bands = (bands + [0.0] * BANDS)[:BANDS]
            self._preamp = _clamp(float(saved.get('preamp', 0.0)), DB_MIN, DB_MAX)
            self._on = bool(saved.get('on', True))   # older saves have no 'on'
        else:
            # A profile with no remembered EQ starts flat (and enabled).
            self._bands = [0.0] * BANDS
            self._preamp = 0.0
            self._on = True
        self._apply()
        self.update()

    def _apply_preset(self, values, preamp=0.0):
        bands = [_clamp(float(v), DB_MIN, DB_MAX) for v in values]
        self._bands = (bands + [0.0] * BANDS)[:BANDS]
        self._preamp = _clamp(float(preamp), DB_MIN, DB_MAX)
        if not self._on:                 # a preset implies the EQ is wanted
            self._on = True
        self._apply()
        self._save_eq_profile()
        self.update()

    # -------------------------------------------------------------- paint
    def paintEvent(self, event):
        p = QPainter(self)
        s = self._scale()
        if s != 1:
            p.scale(s, s)
        active = self.isActiveWindow()

        # Windowshade: the equalizer collapsed to its dedicated compact titlebar.
        # Winamp ships a distinct graphic for this in eq_ex.bmp (decorative bars +
        # a recessed groove, no window controls' "EQUALIZER" text); skins that
        # omit it (e.g. Tenchi) fall back to the full-window titlebar.
        if self._collapsed:
            if self._has_eqex():
                ey = EQEX_TB_ACTIVE if active else EQEX_TB_INACTIVE
                p.drawImage(0, 0, self.skin.sprite(EQEX, 0, ey, W, TITLE_H))
            else:
                p.drawImage(0, 0, self.skin.sprite('eqmain.bmp', 0, 134 if active else 149, W, TITLE_H))
            p.end()
            return

        p.drawImage(0, 0, self.skin.sprite('eqmain.bmp', 0, 0, W, H))
        p.drawImage(0, 0, self.skin.sprite('eqmain.bmp', 0, 134 if active else 149, W, TITLE_H))

        # Album art fills the area to the right of the (fixed-width) EQ face.
        if self._lw() > W:
            self._paint_album_art(p)

        # ON button.
        sx, sy = (69, 119) if self._on else (10, 119)
        p.drawImage(ON_BTN.x(), ON_BTN.y(),
                    self.skin.sprite('eqmain.bmp', sx, sy, ON_BTN.width(), ON_BTN.height()))

        # AUTO button (auto-select a genre preset from the track's metadata).
        ax, ay = (94, 119) if self._auto else (35, 119)
        p.drawImage(AUTO_BTN.x(), AUTO_BTN.y(),
                    self.skin.sprite('eqmain.bmp', ax, ay, AUTO_BTN.width(), AUTO_BTN.height()))

        # Presets button (a sprite in most skins; some also bake it into the bg).
        p.drawImage(PRESETS_BTN.x(), PRESETS_BTN.y(),
                    self.skin.sprite('eqmain.bmp', 224, 164, PRESETS_BTN.width(), PRESETS_BTN.height()))

        # Response-curve graph.
        self._paint_curve(p)

        # Slider wells (the colored VU bar behind each thumb), if the skin has them.
        if self._has_wells():
            wx = self._well_x_offset()
            p.drawImage(self._col_x(-1) + wx, WELL_TOP, self._well_sprite(self._band_frame(self._preamp)))
            for i in range(BANDS):
                p.drawImage(self._col_x(i) + wx, WELL_TOP,
                            self._well_sprite(self._band_frame(self._bands[i])))

        # Sliders (preamp + 10 bands).
        thumb = self.skin.sprite('eqmain.bmp', 0, 176 if self._drag is not None else 164,
                                  THUMB_W, THUMB_H)
        p.drawImage(self._col_x(-1), self._value_to_y(self._preamp), thumb)
        for i in range(BANDS):
            p.drawImage(self._col_x(i), self._value_to_y(self._bands[i]), thumb)
        p.end()

    def _paint_album_art(self, p):
        gap = self._lw() - W
        side = min(gap, H)                 # square, bounded by the EQ height
        ax = W + (gap - side) // 2         # centered in the available gap
        ay = (H - side) // 2
        p.fillRect(W, 0, gap, H, Qt.black)
        song = getattr(self.ctl, 'current_song', None)
        art = getattr(song, 'art_pixbuf', None) if song is not None else None
        if art is not None and not art.isNull():
            scaled = art.scaled(side, side, Qt.KeepAspectRatioByExpanding,
                                Qt.SmoothTransformation)
            p.save()
            p.setClipRect(ax, ay, side, side)
            p.drawPixmap(ax - (scaled.width() - side) // 2,
                         ay - (scaled.height() - side) // 2, scaled)
            p.restore()

    def _skin_accent(self):
        """The skin's analyzer base color (VISCOLOR entry 2) — the same color the
        main window uses for the analyzer/scope/position bar. Used to theme the EQ
        curve when the skin defines no graph line colors of its own."""
        pal = []
        for line in self.skin.text('viscolor.txt').splitlines():
            parts = [x.strip() for x in line.split('//')[0].split(',')]
            if len(parts) >= 3 and parts[0]:
                try:
                    pal.append(QColor(int(parts[0]), int(parts[1]), int(parts[2])))
                except ValueError:
                    pass
        return pal[2] if len(pal) >= 3 else QColor(20, 200, 90)

    def _curve_colors(self):
        """19 per-row colors for the EQ response curve. Winamp skins bake a
        1px-wide, 19px-tall line-color strip into eqmain.bmp at (115, 294); use it
        when it's a real gradient. Skins that leave it flat/near-background (e.g.
        Glare) get the skin's theme accent instead of a hardcoded green."""
        if self._curve_colors_cache is not None:
            return self._curve_colors_cache
        img = self.skin.image('eqmain.bmp')
        strip = []
        if not img.isNull() and img.width() > 115 and img.height() >= 294 + GRAPH.height():
            strip = [img.pixelColor(115, 294 + row) for row in range(GRAPH.height())]
        distinct = {c.getRgb()[:3] for c in strip}
        if len(distinct) > 1:
            colors = strip                          # skin's own line gradient
        else:
            u = strip[0] if strip else None
            spread = max(u.red(), u.green(), u.blue()) - min(u.red(), u.green(), u.blue()) if u else 0
            vivid = u is not None and (spread > 30 or min(u.red(), u.green(), u.blue()) > 80)
            base = u if vivid else self._skin_accent()
            colors = [base] * GRAPH.height()
        self._curve_colors_cache = colors
        return colors

    def _paint_curve(self, p):
        colors = self._curve_colors()
        last = len(colors) - 1
        h, w = GRAPH.height(), GRAPH.width()
        ys = []
        for i in range(BANDS):
            db = self._bands[i] if self._on else 0.0
            f = _clamp((DB_RANGE - db) / (2 * DB_RANGE), 0.0, 1.0)
            ys.append(f * (h - 1))
        p.save()
        p.setClipRect(GRAPH)
        prev = None
        for x in range(w):
            t = x * (BANDS - 1) / max(1, w - 1)     # interpolate between band anchors
            i0 = int(t)
            i1 = min(BANDS - 1, i0 + 1)
            frac = t - i0
            row = max(0, min(h - 1, int(round(ys[i0] * (1 - frac) + ys[i1] * frac))))
            if prev is None:
                p.fillRect(GRAPH.x() + x, GRAPH.y() + row, 1, 1, colors[min(last, row)])
            else:                                    # fill the vertical span for a solid line
                lo, hi = (prev, row) if prev <= row else (row, prev)
                for r in range(lo, hi + 1):
                    p.fillRect(GRAPH.x() + x, GRAPH.y() + r, 1, 1, colors[min(last, r)])
            prev = row
        p.restore()

    # -------------------------------------------------------------- mouse
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = (event.position() / self._scale()).toPoint()
        if CLOSE_BTN.contains(pos):
            self._close_panel()
            return
        if MIN_BTN.contains(pos):
            self._toggle_collapse()
            return
        if ON_BTN.contains(pos):
            self._on = not self._on
            self._apply()
            self._save_eq_profile()
            self.update()
            return
        if AUTO_BTN.contains(pos):
            self._auto = not self._auto
            self.ctl.settings['eq-auto'] = self._auto
            if self._auto:
                self._apply_auto_genre()     # classify the current track now
            self.update()
            return
        if PRESETS_BTN.contains(pos):
            self._show_presets_menu(event.globalPosition().toPoint())
            return
        i = self._slider_at(pos)
        if i is not None:
            self._drag = i
            self._set_from_y(i, pos.y())
        elif pos.y() < TITLE_H:
            shell = self.window()
            # Classic: tear the EQ off the stack (magnetically re-snaps). Modern:
            # the panels are one unit, so drag the whole shell via the compositor.
            if getattr(shell, 'mode', 'modern') == 'classic':
                self._titledrag = True
                shell.start_free_drag(self, event.globalPosition().toPoint())
            else:
                handle = shell.windowHandle()
                if handle is not None:
                    handle.startSystemMove()

    def contextMenuEvent(self, event):
        self._show_presets_menu(event.globalPos())   # right-click -> presets

    def wheelEvent(self, event):
        self.window().wheelEvent(event)               # scroll -> volume

    def mouseMoveEvent(self, event):
        if getattr(self, '_titledrag', False):
            self.window().free_drag_move(self, event.globalPosition().toPoint())
            return
        if self._drag is not None:
            self._set_from_y(self._drag, (event.position() / self._scale()).y())
        else:
            self._update_cursor((event.position() / self._scale()).toPoint())

    def mouseReleaseEvent(self, event):
        if getattr(self, '_titledrag', False):
            self._titledrag = False
            self.window().end_free_drag()
            return
        if self._drag is not None:       # a band/preamp drag just finished
            self._drag = None
            self._save_eq_profile()
        self.update()

    def _set_from_y(self, i, y):
        db = _clamp(self._y_to_value(y), DB_MIN, DB_MAX)
        if i == -1:
            self._preamp = db
        else:
            self._bands[i] = db
        if self._on:
            self._apply()         # re-push all bands (preamp is folded in)
        self.update()
