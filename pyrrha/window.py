# -*- coding: utf-8 -*-
# Pyrrha - a Qt port of Pithos.
# Ported from Pithos' pithos.py (C) 2010-2012 Kevin Mehall and contributors.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3.

"""The main Pyrrha window.

This is a faithful port of Pithos' ``PithosWindow`` to Qt.  The controller
logic (Pandora connection flow, the playback state machine, playlist fetching,
ratings) is preserved almost verbatim; only the *view* changes from GTK widgets
to Qt.  The GStreamer pipeline is built exactly as in Pithos and its bus is
watched through the GLib main context, which
:class:`pyrrha.glib_bridge.GLibBridge` keeps serviced from the Qt event loop.
"""

import html
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from enum import Enum

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstAudio', '1.0')
gi.require_version('GstPbutils', '1.0')
from gi.repository import Gst, GstAudio, GstPbutils, GLib

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QListView, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSlider, QStatusBar, QStyle, QToolButton,
    QVBoxLayout, QWidget,
)

from .pandora import make_pandora
from .pandora import (
    PandoraError, PandoraAuthTokenInvalid, PandoraAPIVersionError,
    RATE_BAN, RATE_LOVE, RATE_NONE,
)
from .pandora.pandora import Song
from .pandora.data import (
    client_keys, default_client_id, default_one_client_id,
)

from . import __version__
from . import local
from . import radio
from . import radiobrowser
from .settings import get_settings
from .appicon import app_icon
from .keyring import SecretService, is_flatpak
from .models import ALBUM_ART_SIZE, AlbumArtDelegate, SongsModel, SongRole
from .stations_popover import StationsPopover
from .worker import Worker
from .plugin_loader import load_plugins
from .dialogs.preferences import PreferencesDialog
from .dialogs.stations import StationsDialog
from .dialogs.radio import RadioDialog
from .dialogs.about import AboutDialog

try:
    import pacparser
except ImportError:
    pacparser = None

try:
    RESAMPLER_QUALITY_MAX = GstAudio.AUDIO_RESAMPLER_QUALITY_MAX
    RESAMPLER_FILTER_MODE_FULL = GstAudio.AudioResamplerFilterMode.FULL
except AttributeError:
    RESAMPLER_QUALITY_MAX = 10
    RESAMPLER_FILTER_MODE_FULL = 1

# 15 days in seconds to retain album art files.
ART_CACHE_TIME = 1.296e+6

# Spectrum analyzer (drives the skinned Winamp visualizer). Magnitudes are
# posted as dB in [SPECTRUM_THRESHOLD, 0]; the skinned window reads
# ``spectrum_bands`` (normalized 0..1).
SPECTRUM_BANDS = 256
SPECTRUM_THRESHOLD = -80
_SPECTRUM_RE = re.compile(r'magnitude=\(float\)\{([^}]*)\}')

# When jumping directly to a later Pandora song, a current song that played
# less than this many seconds is discarded rather than kept as history.
JUMP_TRIM_SECONDS = 45

# Playback sources. Pandora is an endless fetched station; Local and Radio are
# static playlists (of files / of favourite stations). Pandora and Radio both
# stream over the network (buffering); Local plays immediately. See the
# ``local_mode`` / ``static_playlist`` / ``streaming`` window properties.
SOURCE_PANDORA = 'pandora'
SOURCE_LOCAL = 'local'
SOURCE_RADIO = 'radio'

# How many times to silently reconnect a dropped radio stream before giving up.
RADIO_RECONNECT_TRIES = 3

# How often to prune Pandora songs whose audio URLs have expired (seconds).
PANDORA_PRUNE_INTERVAL = 60


def _song_still_valid(song):
    """Whether a queued song is still playable (Pandora URLs expire after an
    hour). Local/radio songs are always valid. Never raises."""
    try:
        return bool(song.is_still_valid())
    except Exception:
        return False


def parse_proxy(proxy):
    """``_parse_proxy`` from urllib, copied from Pithos' util so we never pull
    GTK into the Qt process just to configure the GStreamer source's proxy."""
    from urllib.parse import splittype, splituser, splitpasswd
    scheme, r_scheme = splittype(proxy)
    if not r_scheme.startswith("/"):
        scheme = None
        authority = proxy
    else:
        if not r_scheme.startswith("//"):
            raise ValueError("proxy URL with no authority: %r" % proxy)
        end = r_scheme.find("/", 2)
        if end == -1:
            end = None
        authority = r_scheme[2:end]
    userinfo, hostport = splituser(authority)
    if userinfo is not None:
        user, password = splitpasswd(userinfo)
    else:
        user = password = None
    return scheme, user, password, hostport


class PseudoGst(Enum):
    """Aliases to Gst.State plus our own BUFFERING pseudo-state (as in Pithos)."""
    PLAYING = 1
    PAUSED = 2
    BUFFERING = 3
    STOPPED = 4

    @property
    def state(self):
        return {
            1: Gst.State.PLAYING,
            2: Gst.State.PAUSED,
            3: Gst.State.PAUSED,
            4: Gst.State.NULL,
        }[self.value]


def _dropped_skin_paths(mime):
    """Local skin paths (.wsz/.zip or folders with a main.bmp) from drag mime."""
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


class PyrrhaWindow(QMainWindow):
    # Signals mirroring Pithos' gsignals, for the (future) plugin layer.
    song_changed = Signal(object)
    song_ended = Signal(object)
    play_state_changed = Signal(bool)
    user_changed_play_state = Signal(bool)
    metadata_changed = Signal(object)
    buffering_finished = Signal(object)
    station_changed_sig = Signal(object)
    source_changed_sig = Signal(str)       # entered Local / Radio / Pandora
    stations_processed = Signal(object)
    station_added_sig = Signal(object)     # Station
    station_removed_sig = Signal(object)   # Station
    station_renamed_sig = Signal(object)   # (station_id, new_name)
    songs_added = Signal(int)

    def __init__(self, app, test_mode=False):
        super().__init__()
        self.app = app
        self.setWindowTitle('Pyrrha')
        self.setWindowIcon(app_icon())

        self.settings = get_settings()
        self.settings.changed.connect(self._on_setting_changed)

        self.prefs_dlg = PreferencesDialog(self)
        self.prefs_dlg.login_changed.connect(self.pandora_reconnect)
        self.prefs_dlg.applied.connect(self.on_explicit_content_filter_checkbox)
        self.prefs_dlg.finished.connect(self.on_prefs_finished)

        self.stations_dlg = None
        self.radio_dlg = None
        # Plugins may register callables here to intercept window close
        # (return True to hide instead of quit); see closeEvent.
        self.close_interceptors = []

        self.init_core()
        self.init_ui()
        self.init_actions()

        self.plugins = {}
        load_plugins(self)

        self.pandora = make_pandora(test_mode)
        self.set_proxy(reconnect=False)
        self.set_audio_quality()
        SecretService.unlock_keyring(self.on_keyring_unlocked)

    # ------------------------------------------------------------------ core
    def init_core(self):
        # Station rows shared with the dialogs: list of [station, name, index].
        self.stations_model = []

        Gst.init(None)
        self._query_duration = Gst.Query.new_duration(Gst.Format.TIME)
        self._query_position = Gst.Query.new_position(Gst.Format.TIME)
        self._query_buffer = Gst.Query.new_buffering(Gst.Format.PERCENT)

        self.player = Gst.ElementFactory.make("playbin3", "player")
        self.player.set_property('buffer-duration', 3 * Gst.SECOND)
        self.rgvolume = Gst.ElementFactory.make("rgvolume", "rgvolume")
        self.rgvolume.set_property("album-mode", False)
        self.rglimiter = Gst.ElementFactory.make("rglimiter", "rglimiter")
        self.rglimiter.set_property("enabled", False)
        self.equalizer = Gst.ElementFactory.make("equalizer-10bands", "equalizer-10bands")
        # Stereo balance/pan for the skinned main window (optional element).
        self.panorama = Gst.ElementFactory.make("audiopanorama", "audiopanorama")
        # Spectrum analyzer feeding the skinned visualizer (optional element).
        self.spectrum_bands = [0.0] * SPECTRUM_BANDS
        self.spectrum = Gst.ElementFactory.make("spectrum", "spectrum")
        if self.spectrum is not None:
            self.spectrum.set_property("bands", SPECTRUM_BANDS)
            self.spectrum.set_property("threshold", SPECTRUM_THRESHOLD)
            self.spectrum.set_property("interval", 50 * Gst.MSECOND)
            self.spectrum.set_property("post-messages", True)
            self.spectrum.set_property("message-magnitude", True)
            self.spectrum.set_property("message-phase", False)
        audioconvert = Gst.ElementFactory.make("audioconvert", "audioconvert")
        audioresample = Gst.ElementFactory.make("audioresample", "audioresample")
        audioresample.set_property("quality", RESAMPLER_QUALITY_MAX)
        audioresample.set_property("sinc-filter-mode", RESAMPLER_FILTER_MODE_FULL)
        audiosink = Gst.ElementFactory.make("autoaudiosink", "audiosink")
        sinkbin = Gst.Bin()
        chain = [self.rgvolume, self.rglimiter, self.equalizer]
        if self.panorama is not None:
            chain.append(self.panorama)
        if self.spectrum is not None:
            chain.append(self.spectrum)
        chain += [audioconvert, audioresample, audiosink]
        for element in chain:
            sinkbin.add(element)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        sinkbin.add_pad(Gst.GhostPad.new("sink", self.rgvolume.get_static_pad("sink")))
        self.player.set_property("audio-sink", sinkbin)

        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message::stream-start", self.on_gst_stream_start)
        bus.connect("message::tag", self.on_gst_tag)
        bus.connect("message::eos", self.on_gst_eos)
        bus.connect("message::buffering", self.on_gst_buffering)
        bus.connect("message::error", self.on_gst_error)
        bus.connect("message::element", self.on_gst_element)
        self.player.connect("notify::volume", self.on_gst_volume)
        self.player.connect("notify::source", self.on_gst_source)

        self._current_state = PseudoGst.STOPPED
        self._buffer_recovery_state = PseudoGst.STOPPED

        self.current_song_index = None
        self.current_station = None
        self.current_station_id = self.settings['last-station-id']
        # Active playback source (SOURCE_PANDORA / SOURCE_LOCAL / SOURCE_RADIO).
        # Local and Radio are static playlists (of files / of favourite
        # stations) instead of an endless Pandora station. ``local_mode`` below
        # is derived from this for the many local-only call sites.
        self.source = SOURCE_PANDORA
        self._radio_reconnect_left = 0     # remaining auto-reconnects this stream
        # Persisted Pandora queue: a stash of the current station's songs +
        # position, kept when leaving Pandora (mode switch) and saved to disk on
        # close, so returning/reopening resumes it instead of refetching. Guarded
        # by each song's 1-hour validity (see _prune_expired_songs / restore).
        self._pandora_saved = None         # {'station_id', 'songs', 'index'} or None
        self._pandora_disk_loaded = False  # one-time disk restore on first connect
        self._local_dir = ''   # last folder used in the open dialogs
        # Play order for local playback (persisted; ignored for Pandora).
        self.shuffle = self.settings['shuffle']
        self.repeat = self.settings['repeat']
        # Sleep timer (Winamp "Sleep"): stop playback after a delay or at the
        # end of the current track.
        self._sleep_timer = None
        self._sleep_deadline = None      # monotonic time the timer fires, or None
        self._sleep_minutes = 0          # active preset (0 = off), for the menu
        self._sleep_end_of_track = False
        self._shuffle_order = None   # cached permutation of playlist indices

        self.auto_retrying_auth = False
        self.have_stations = False
        self.playcount = 0
        self.gstreamer_errorcount_1 = 0
        self.gstreamer_errorcount_2 = 0
        self.gstreamer_error = ''
        self.waiting_for_playlist = False
        self.start_new_playlist = False
        self.filter_state = None
        self.buffering_timer_id = 0
        self.ui_loop_timer_id = 0
        self.playlist_update_timer_id = 0
        self.worker = Worker()
        self._status_messages = {}
        self._station_eq = self._load_station_eq()
        # Periodically drop Pandora songs whose audio URLs have expired.
        self._prune_timer_id = GLib.timeout_add_seconds(
            PANDORA_PRUNE_INTERVAL, self._on_prune_timer)
        self.skinned_shell = None     # set by __main__ when launched with --skin
        self._skinned_active = False  # True while the skinned shell is the shown view

        try:
            tempdir_base = '/var/tmp'
            if is_flatpak():
                tempdir_base = os.path.join(GLib.get_user_cache_dir(), 'tmp')
            self.tempdir = os.path.join(tempdir_base, 'pyrrha_cache')
            os.makedirs(self.tempdir, exist_ok=True)
        except IOError as e:
            self.tempdir = None
            logging.warning('Failed to create a temporary directory: {}'.format(e))

    @property
    def playing(self):
        return self._buffer_recovery_state is not PseudoGst.PAUSED

    def _set_source(self, src):
        """Switch the active playback source, announcing the change so mode-scoped
        state (e.g. the EQ's per-mode memory) can follow. No-op/no signal when the
        source is unchanged."""
        if src == self.source:
            return
        self.source = src
        self.source_changed_sig.emit(src)

    @property
    def local_mode(self):
        """True when playing local files. Kept as the historical name for the
        many local-only call sites (queue editing, tag edit, playlist save)."""
        return self.source == SOURCE_LOCAL

    @property
    def is_radio(self):
        """True when playing internet radio (its own source, like Pandora)."""
        return self.source == SOURCE_RADIO

    @property
    def static_playlist(self):
        """True when the song list is a fixed list we own end-to-end (local
        files or favourite radio stations) rather than a fetched Pandora
        station: no prefetch, jumping stops at the ends, prev works."""
        return self.source in (SOURCE_LOCAL, SOURCE_RADIO)

    @property
    def streaming(self):
        """True when playback pulls from the network (Pandora or radio) and so
        goes through the BUFFERING state instead of starting immediately."""
        return self.source in (SOURCE_PANDORA, SOURCE_RADIO)

    PREQUEUE_TARGET = 8   # default; overridden by the 'prequeue-size' setting

    # -------------------------------------------------------------------- ui
    def init_ui(self):
        self.songs_model = SongsModel(self)
        # Keep the Pandora queue topped up: refill on song change and after each
        # fetch completes (chains until the buffer is full).
        self.song_changed.connect(lambda *_: self._ensure_queue_buffer())
        self.songs_added.connect(self._on_songs_added)
        self.songs_view = QListView()
        self.songs_view.setModel(self.songs_model)
        self.songs_view.setItemDelegate(AlbumArtDelegate(self.songs_view))
        self.songs_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.songs_view.setUniformItemSizes(True)
        self.songs_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.songs_view.customContextMenuRequested.connect(self.on_songs_context_menu)
        self.songs_view.doubleClicked.connect(lambda idx: self.start_selected_song())

        # Header: station chooser + transport + volume.
        self.stations_button = QToolButton()
        self.stations_button.setText(_('Choose Station'))
        self.stations_button.setPopupMode(QToolButton.InstantPopup)
        self.stations_button.clicked.connect(self.toggle_stations_popover)

        self.stations_popover = StationsPopover(self)
        self.stations_popover.station_selected.connect(self.station_changed)

        self.playpause_button = QPushButton()
        self.playpause_button.setIcon(self._pause_icon())
        self.playpause_button.setToolTip(_('Play/Pause'))
        self.playpause_button.clicked.connect(self.user_playpause)

        self.skip_button = QPushButton()
        self.skip_button.setIcon(self._skip_icon())
        self.skip_button.setToolTip(_('Skip'))
        self.skip_button.clicked.connect(self.next_song)

        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(100)
        self.volume_slider.setFixedWidth(120)
        self.volume_slider.setValue(int(self.settings['volume'] * 100))
        self.volume_slider.valueChanged.connect(self.on_volume_slider_changed)

        self.menu_button = QToolButton()
        menu_icon = self._icon('open-menu', 'application-menu')
        if menu_icon.isNull():
            self.menu_button.setText('☰')  # trigram / hamburger fallback
        else:
            self.menu_button.setIcon(menu_icon)
        self.menu_button.setPopupMode(QToolButton.InstantPopup)
        self.menu_button.setMenu(self._build_main_menu())
        # Drop a .wsz / skin folder onto the window to install it and open it in
        # the skinned (WinAMP 2.x) view.
        self.setAcceptDrops(True)

        header = QHBoxLayout()
        header.addWidget(self.stations_button)
        header.addWidget(self.playpause_button)
        header.addWidget(self.skip_button)
        header.addStretch(1)
        header.addWidget(self.volume_slider)
        header.addWidget(self.menu_button)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addLayout(header)
        layout.addWidget(self.songs_view)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.resize(500, 550)

        # Apply the persisted volume and balance to the player.
        self.set_player_volume(self.settings['volume'])
        self.set_player_balance(self.settings['balance'])

    def _icon(self, *names):
        for name in names:
            icon = QIcon.fromTheme(name)
            if not icon.isNull():
                return icon
        return QIcon()

    def _themed_or_standard(self, theme_name, standard_pixmap):
        """Themed icon if present, otherwise Qt's built-in style icon (always
        available, so transport buttons are never blank on minimal themes)."""
        icon = QIcon.fromTheme(theme_name)
        if not icon.isNull():
            return icon
        return self.style().standardIcon(standard_pixmap)

    def _pause_icon(self):
        return self._themed_or_standard('media-playback-pause', QStyle.SP_MediaPause)

    def _play_icon(self):
        return self._themed_or_standard('media-playback-start', QStyle.SP_MediaPlay)

    def _skip_icon(self):
        return self._themed_or_standard('media-skip-forward', QStyle.SP_MediaSkipForward)

    def set_skinned_shell(self, shell):
        """Register the Winamp-skinned shell so the two views can toggle."""
        self.skinned_shell = shell
        self._skinned_active = shell is not None

    def active_view(self):
        """The widget currently serving as the player (skinned shell or native)."""
        if self.skinned_shell is not None and self._skinned_active:
            return self.skinned_shell
        return self

    def present_player(self):
        """Show and raise whichever view is the active player (used by the tray)."""
        view = self.active_view()
        if view is self:
            self.bring_to_top()
        else:
            view.showNormal()
            view.raise_()
            view.activateWindow()

    def hide_player(self):
        self.active_view().hide()

    def player_visible(self):
        view = self.active_view()
        return view.isVisible() and not view.isMinimized()

    def show_standard_view(self):
        """Switch from the skinned UI to the standard (native) window. This is
        the dedicated Pandora UI, so leaving local/radio playback for Pandora
        streaming is implied — switch back if we weren't already on Pandora."""
        self._skinned_active = False
        self.settings['skinned-view'] = False   # remember for next start
        if self.skinned_shell is not None:
            self.skinned_shell.hide()
        self.show()
        self.raise_()
        self.activateWindow()
        if self.source != SOURCE_PANDORA:
            self.switch_to_pandora()

    def show_skinned_view(self, force_modern=True):
        """Switch from the standard window to the skinned UI, building the
        skinned shell on first use. ``force_modern`` opens the curated Modern
        view (the menu default); pass False to honor the last-used skin mode,
        as when restoring the saved view at startup. Returns True if the
        skinned view is now shown, False if it could not be built."""
        if self.skinned_shell is None and not self._build_skinned_shell(force_modern):
            return False
        self._skinned_active = True
        self.settings['skinned-view'] = True    # remember for next start
        self.hide()
        self.skinned_shell.show()
        self.skinned_shell.raise_()
        self.skinned_shell.activateWindow()
        return True

    def show_skinned_view_mode(self, mode):
        """Enter the skinned UI directly in a specific mode ('classic' = WinAMP
        2.x, 'modern' = Pyrrha). Sets the saved mode first so a freshly built
        shell opens there, then switches an already-built shell to match."""
        self.set_skin_mode(mode)
        if self.show_skinned_view(force_modern=False) and self.skinned_shell is not None:
            self.skinned_shell.set_mode(mode)

    def dragEnterEvent(self, event):
        if _dropped_skin_paths(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = _dropped_skin_paths(event.mimeData())
        if not paths:
            return
        installed = self.install_skin(paths[0])
        self.set_last_skin(installed)
        # Show it in the WinAMP 2.x view, then apply (in case the shell existed).
        self.show_skinned_view_mode('classic')
        if self.skinned_shell is not None:
            self.skinned_shell.load_skin(installed)
        event.acceptProposedAction()

    def open_visualizer(self):
        """Open the large visualizer window. The skinned shell hosts it, so build
        the shell on first use (kept hidden — this doesn't switch the player view)
        if it doesn't exist yet. Reachable from the standard view's Preferences."""
        if self.skinned_shell is None and not self._build_skinned_shell(force_modern=False):
            return
        self.skinned_shell.open_vis_window()

    def _build_skinned_shell(self, force_modern=True):
        """Create the Winamp-skinned shell on demand from the last-used skin, or
        the first available one (bundled or in the user's skins dir). Returns
        True on success; warns and returns False if none can be loaded."""
        path = self.get_last_skin()
        if not path or not os.path.exists(path):
            skins = self.available_skins()
            path = skins[0][1] if skins else None
        if not path:
            QMessageBox.information(
                self, _('Winamp Skin'),
                _('No skins are available. Add a .wsz skin or a skin folder to '
                  '{}.').format(self.skins_dir()))
            return False
        if force_modern:
            # Open the curated Modern (album-art) view by default; Classic stays
            # a click away in the skinned window's mode menu.
            self.set_skin_mode('modern')
        try:
            from .skinned.skin import Skin
            from .skinned.window import SkinnedShell
            shell = SkinnedShell(self, Skin(path))
        except Exception as e:
            logging.warning('Failed to load skin %s: %s', path, e)
            QMessageBox.warning(
                self, _('Winamp Skin'),
                _('Failed to load the skin:\n{}').format(e))
            return False
        self.set_skinned_shell(shell)
        self.set_last_skin(path)
        return True

    def _build_main_menu(self):
        # The standard window is the dedicated Pandora UI, so it stays
        # Pandora-oriented; entering it forces Pandora streaming (see
        # show_standard_view). Local playback lives in the skinned views.
        menu = QMenu(self)
        menu.addAction(_('Stations…'), self.show_stations, QKeySequence('Ctrl+S'))
        menu.addAction(_('Internet Radio…'), self.show_radio, QKeySequence('Ctrl+Shift+R'))
        menu.addAction(_('Preferences…'), self.show_preferences, QKeySequence('Ctrl+P'))
        menu.addSeparator()
        ui_menu = menu.addMenu(_('UI'))
        ui_menu.addAction(_('WinAMP 2.x'), lambda: self.show_skinned_view_mode('classic'))
        ui_menu.addAction(_('Pyrrha'), lambda: self.show_skinned_view_mode('modern'))
        menu.addSeparator()
        menu.addAction(_('Help'), lambda: self.open_url('https://github.com/k5blazerfl/Pyrrha'))
        menu.addAction(_('About'), self.show_about)
        menu.addSeparator()
        menu.addAction(_('Quit'), self.close, QKeySequence('Ctrl+Q'))
        return menu

    def init_actions(self):
        def shortcut(seq, slot):
            act = QAction(self)
            act.setShortcut(QKeySequence(seq))
            act.triggered.connect(slot)
            self.addAction(act)
            return act

        shortcut('Space', self.user_playpause)
        shortcut('Return', self.start_selected_song)
        shortcut('Ctrl+I', self.info_song)
        shortcut('Ctrl+Up', self.volume_up)
        shortcut('Ctrl+Down', self.volume_down)
        shortcut('Ctrl+Right', self.next_song)
        shortcut('Ctrl+L', self.love_song)
        shortcut('Ctrl+B', self.ban_song)
        shortcut('Ctrl+T', self.tired_song)
        shortcut('Ctrl+U', self.unrate_song)
        shortcut('Ctrl+D', self.bookmark_song)
        shortcut('Ctrl+R', self.toggle_stations_popover)

    # ------------------------------------------------------------- status bar
    def status_push(self, context, message):
        self._status_messages[context] = message
        self.statusBar().showMessage(message)

    def status_pop(self, context):
        self._status_messages.pop(context, None)
        if self._status_messages:
            self.statusBar().showMessage(next(reversed(self._status_messages.values())))
        else:
            self.statusBar().clearMessage()

    # -------------------------------------------------------------- keyring
    def on_keyring_unlocked(self, error):
        if error:
            logging.error('You need to install a service such as gnome-keyring. Error: {}'.format(error))
            self.fatal_error_dialog(
                getattr(error, 'message', str(error)),
                _('You need to install a service such as gnome-keyring.'),
            )
        else:
            self.pandora_connect()

    # --------------------------------------------------------------- worker
    def worker_run(self, fn, args=(), callback=None, message=None, context='net',
                   errorback=None, user_data=None):
        if context and message:
            self.status_push(context, message)

        if isinstance(fn, str):
            fn = getattr(self.pandora, fn)

        def cb(v):
            if context:
                self.status_pop(context)
            if callback:
                if user_data is not None:
                    callback(v, user_data)
                else:
                    callback(v)

        def eb(e):
            if context and message:
                self.status_pop(context)

            def retry_cb():
                self.auto_retrying_auth = False
                if fn is not self.pandora.connect:
                    self.worker_run(fn, args, callback, message, context)

            if isinstance(e, PandoraAuthTokenInvalid) and not self.auto_retrying_auth:
                self.auto_retrying_auth = True
                logging.info("Automatic reconnect after invalid auth token")
                self.pandora_connect(message="Reconnecting...", callback=retry_cb)
            elif isinstance(e, PandoraAPIVersionError):
                self.api_update_dialog()
            elif isinstance(e, PandoraError):
                self.error_dialog(e.message, retry_cb, submsg=e.submsg)
            else:
                logging.warning(getattr(e, 'traceback', e))

        self.worker.send(fn, args, cb, errorback or eb)

    # ---------------------------------------------------------------- proxy
    def get_proxy(self):
        proxy = self.settings['proxy']
        if proxy:
            return proxy
        system_proxies = urllib.request.getproxies()
        return system_proxies.get('http')

    def _on_setting_changed(self, key):
        # Stands in for GSettings' per-key ``changed::`` handlers: one slot on
        # the ``changed`` signal, dispatched by key.
        if key == 'audio-quality':
            self.set_audio_quality()
        elif key in ('proxy', 'control-proxy', 'control-proxy-pac'):
            self.set_proxy()

    def set_proxy(self, *ignore, reconnect=True):
        from .pandora import pandora

        handlers = []
        global_proxy = self.settings['proxy']
        if global_proxy:
            handlers.append(urllib.request.ProxyHandler(
                {'http': global_proxy, 'https': global_proxy}))
        global_opener = pandora.Pandora.build_opener(*handlers)
        urllib.request.install_opener(global_opener)

        control_opener = global_opener
        control_proxy = self.settings['control-proxy']
        control_proxy_pac = self.settings['control-proxy-pac']

        if not control_proxy and (control_proxy_pac and pacparser):
            pacparser.init()
            with urllib.request.urlopen(control_proxy_pac) as f:
                pacstring = f.read().decode('utf-8')
                try:
                    pacparser.parse_pac_string(pacstring)
                except pacparser._pacparser.error:
                    logging.warning('Failed to parse PAC.')
            try:
                proxies = pacparser.find_proxy("http://pandora.com", "pandora.com").split(";")
                for proxy in proxies:
                    match = re.search("PROXY (.*)", proxy)
                    if match:
                        control_proxy = match.group(1)
                        break
            except pacparser._pacparser.error:
                logging.warning('Failed to find proxy via PAC.')
            pacparser.cleanup()
        elif not control_proxy and (control_proxy_pac and not pacparser):
            logging.warning("Disabled proxy auto-config support because python-pacparser module was not found.")

        if control_proxy:
            control_opener = pandora.Pandora.build_opener(
                urllib.request.ProxyHandler({'http': control_proxy, 'https': control_proxy}))

        self.pandora.set_url_opener(control_opener)
        if reconnect:
            self.pandora_connect()

    def set_audio_quality(self, *ignore):
        self.pandora.set_audio_quality(self.settings['audio-quality'])

    # ------------------------------------------------------------- pandora
    def pandora_connect(self, *ignore, message="Logging in...", callback=None):
        def cb(password):
            if not password:
                self.show_preferences()
            else:
                self._pandora_connect_real(message, callback, email, password)

        email = self.settings['email']
        if not email:
            self.show_preferences()
        else:
            SecretService.get_account_password(email, cb)

    def _pandora_connect_real(self, message, callback, email, password):
        if self.settings['pandora-one']:
            client = client_keys[default_one_client_id]
        else:
            client = client_keys[default_client_id]

        force_client = self.settings['force-client']
        if force_client in client_keys:
            client = client_keys[force_client]
        elif force_client and force_client[0] == '{':
            try:
                client = json.loads(force_client)
            except json.JSONDecodeError:
                logging.error("Could not parse force_client json")

        args = (client, email, password)

        def on_got_stations(*ignore):
            self.process_stations()
            if callback:
                callback()

        def pandora_ready(*ignore):
            logging.info("Pandora connected")
            if self.settings['pandora-one'] != self.pandora.isSubscriber:
                self.settings['pandora-one'] = self.pandora.isSubscriber
                self._pandora_connect_real(message, callback, email, password)
            else:
                self.worker_run('get_stations', (), on_got_stations, 'Getting stations...', 'login')

        self.worker_run('connect', args, pandora_ready, message, 'login')

    def pandora_reconnect(self, email_password):
        email, password = email_password
        self.stop()
        self.waiting_for_playlist = False
        self.current_song_index = None
        self.start_new_playlist = False
        self.current_station = None
        self.current_station_id = None
        self.have_stations = False
        self.playcount = 0
        self.songs_model.clear()
        self._pandora_connect_real("Logging in...", None, email, password)

    # ---------------------------------------------- explicit content filter
    def on_explicit_content_filter_checkbox(self, *ignore):
        if not self.pandora.connected:
            return
        current_checkbox_state = self.prefs_dlg.explicit_filter_checked()

        def set_content_filter(current_state):
            self.pandora.set_explicit_content_filter(current_state)

        def get_new_playlist(*ignore):
            if current_checkbox_state:
                logging.info('Getting a new playlist.')
                self.waiting_for_playlist = False
                self.stop()
                self.current_song_index = None
                self.songs_model.clear()
                self.get_playlist(start=True)

        if self.filter_state is not None and self.filter_state != current_checkbox_state:
            self.worker_run(set_content_filter, (current_checkbox_state,), get_new_playlist)

    def sync_explicit_content_filter_setting(self, *ignore):
        self.prefs_dlg.set_filter_unknown()
        self.filter_state = None
        if not self.pandora.connected:
            return

        def get_state(*ignore):
            return self.pandora.explicit_content_filter_state

        def sync_checkbox(current_state):
            self.filter_state, pin_protected = current_state[0], current_state[1]
            self.prefs_dlg.set_filter_state(self.filter_state, pin_protected)

        self.worker_run(get_state, (), sync_checkbox)

    # -------------------------------------------------------------- stations
    def station_rows(self):
        return self.stations_model

    def process_stations(self, *ignore):
        # First connect of the session: pull a saved queue off disk so the
        # station selected below resumes it instead of fetching (paused).
        if not self._pandora_disk_loaded:
            self._pandora_disk_loaded = True
            self._load_pandora_queue_from_disk()
        self.stations_model = []
        self.current_station = None
        selected = None
        for i, s in enumerate(self.pandora.stations):
            if s.isThumbprint:
                self.pandora.stations.insert(1, self.pandora.stations.pop(i))
                break
        for i, s in enumerate(self.pandora.stations):
            if s.isQuickMix and s.isCreator:
                self.stations_model.append([s, "QuickMix", i])
            else:
                self.stations_model.append([s, s.name, i])
            if s.id == self.current_station_id:
                logging.info("Restoring saved station: id = %s" % (s.id))
                selected = s
        if not selected and self.stations_model:
            selected = self.stations_model[0][0]

        self._refresh_stations_popover()

        if selected:
            self.station_changed(selected, reconnecting=self.have_stations)
            self.have_stations = True
            self.stations_processed.emit(self.pandora.stations)
        else:
            self.show_stations()

    def _refresh_stations_popover(self):
        self.stations_popover.set_rows(
            (row[0], row[1], row[2]) for row in self.stations_model)
        if self.current_station:
            self.stations_popover.select_station(self.current_station)

    def toggle_stations_popover(self, *ignore):
        self.stations_popover.toggle_visibility(self.stations_button)

    def station_changed(self, station, reconnecting=False):
        if station is self.current_station:
            return
        self.source = SOURCE_PANDORA   # picking a station leaves local/radio
        self.waiting_for_playlist = False
        if not reconnecting:
            self.stop()
            self.current_song_index = None
            self.songs_model.clear()
        logging.info("Selecting station %s; total = %i" % (station.id, len(self.stations_model)))
        self.current_station_id = station.id
        self.current_station = station
        self.settings.set_string('last-station-id', self.current_station_id)
        if not reconnecting:
            # Resume a saved queue for this station (mode switch or restart)
            # instead of fetching a fresh playlist; only fetch if there's nothing
            # valid to restore.
            if not self._restore_pandora_queue(station):
                self.get_playlist(start=True)
        self.stations_button.setText(station.name)
        self.stations_popover.select_station(station)
        self.station_changed_sig.emit(station)

    def on_station_renamed(self, station, new_name):
        for row in self.stations_model:
            if row[0] is station:
                row[1] = new_name
                break
        self._refresh_stations_popover()
        if station is self.current_station:
            self.stations_button.setText(new_name)
        self.station_renamed_sig.emit((station.id, new_name))

    def on_station_added(self, station, user_data, source=None):
        music_type, description = user_data
        for row in self.stations_model:
            if row[0].id == station.id:
                self.station_already_exists(row[0], description, music_type)
                return
        self.pandora.stations.append(station)
        self.stations_model.insert(0, [station, station.name, 0])
        self._refresh_stations_popover()
        self.station_added_sig.emit(station)
        self.station_changed(station)
        if source is not None and hasattr(source, 'reload'):
            source.reload()

    def station_already_exists(self, station, description, music_type):
        sub_title = _('Pandora does not permit multiple stations with the same seed.')
        if music_type == 'song':
            seed = _('Song Seed:')
        elif music_type == 'artist':
            seed = _('Artist Seed:')
        else:
            seed = _('Genre Seed:')

        if station is self.current_station:
            QMessageBox.information(
                self, _('A New Station could not be created'),
                _('{0}\n"{1}", the Station you are currently listening to already '
                  'contains the {2} {3}.').format(sub_title, station.name, seed, description))
        else:
            reply = QMessageBox.question(
                self, _('A New Station could not be created'),
                _('{0}\nYour Station "{1}" already contains the {2} {3}.\n'
                  'Would you like to listen to it now?').format(
                    sub_title, station.name, seed, description))
            if reply == QMessageBox.Yes:
                self.station_changed(station)

    def remove_station(self, station):
        self.stations_model = [row for row in self.stations_model if row[0] is not station]
        self._refresh_stations_popover()
        self.station_removed_sig.emit(station)

    def refresh_stations(self, *ignore):
        self.worker_run(self.pandora.get_stations, (), self.process_stations,
                        "Refreshing stations...")

    # --------------------------------------------------------------- current
    @property
    def current_song(self):
        if self.current_song_index is not None:
            return self.songs_model.song_at(self.current_song_index)

    def start_song(self, song_index, jump=False):
        if self.source == SOURCE_RADIO:
            # A station whose URL is a .pls/.m3u/.asx playlist must be unwrapped
            # to the real stream URL before playbin gets it. Do it once, lazily,
            # off the GUI thread, then re-enter start_song with the resolved URL.
            song = self.songs_model.song_at(song_index)
            if song is not None and getattr(song, '_needs_resolve', False):
                song._needs_resolve = False
                self._resolve_and_start(song_index, song, jump)
                return
        if self.static_playlist:
            # Static playlist (local files / radio favourites): stop at the ends
            # instead of fetching more.
            if not (0 <= song_index < len(self.songs_model)):
                return self.stop()
        else:
            # A direct user jump to a later song discards what it leaps past:
            # the queued songs between the current one and the target, plus the
            # current song itself if it barely played (< JUMP_TRIM_SECONDS).
            # Songs listened to for longer stay as history. Done before the
            # pre-fetch check so the remaining-count math sees the trimmed queue.
            if (jump and self.current_song_index is not None
                    and song_index > self.current_song_index):
                cur = self.current_song_index
                pos = self.query_position() or 0
                drop_current = pos < JUMP_TRIM_SECONDS * Gst.SECOND
                lo = cur if drop_current else cur + 1
                for r in range(song_index - 1, lo - 1, -1):
                    self.songs_model.remove_row(r)
                song_index -= (song_index - lo)
                self.current_song_index = None if drop_current else cur
                self._reindex_songs()
            songs_remaining = len(self.songs_model) - song_index
            if songs_remaining <= 0:
                return self.get_playlist(start=True)
            elif songs_remaining == 1:
                self.get_playlist()

        prev = self.current_song
        self.stop()
        self.current_song_index = song_index
        if prev:
            self.update_song_row(prev)

        if not self.current_song.is_still_valid():
            self.current_song.message = 'Song expired'
            self.update_song_row()
            return self.next_song()

        if self.current_song.tired or self.current_song.rating == RATE_BAN:
            return self.next_song()

        logging.info("Starting song: index = %i" % (song_index))
        song = self.current_song
        audioUrl = song.audioUrl
        os.environ['PULSE_PROP_media.title'] = song.title
        os.environ['PULSE_PROP_media.artist'] = song.artist
        os.environ['PULSE_PROP_media.name'] = '{}: {}'.format(song.artist, song.title)
        os.environ['PULSE_PROP_media.filename'] = audioUrl
        if song.bitrate:   # network tuning; irrelevant (and None) for local files
            self.player.set_property('buffer-size', int(song.bitrate) * 375)
            self.player.set_property('connection-speed', int(song.bitrate))
        self.player.set_property("uri", audioUrl)
        if self.local_mode:
            # Local files don't emit network buffering messages, so the
            # BUFFERING→PLAYING transition (driven by message::buffering) never
            # fires — play immediately instead.
            self._set_player_state(PseudoGst.PLAYING, change_gst_state=True)
        else:
            # Pandora and radio stream over the network → buffer first.
            self._set_player_state(PseudoGst.BUFFERING)
        if self.source == SOURCE_RADIO:
            # Fresh stream: restore the reconnect budget for drop recovery.
            self._radio_reconnect_left = RADIO_RECONNECT_TRIES
        self.playcount += 1

        self.current_song.start_time = time.time()
        idx = self.songs_model.index(song_index)
        self.songs_view.scrollTo(idx, QAbstractItemView.PositionAtBottom)
        self.songs_view.setCurrentIndex(idx)
        self.setWindowTitle("%s by %s - Pyrrha" % (song.title, song.artist))
        self.update_song_row()
        self.song_changed.emit(song)
        self.metadata_changed.emit(song)

    def remove_song(self, row):
        """Remove a song from a static playlist (local files / radio
        favourites). Re-indexes the remaining songs and adjusts the current
        index; removing the playing song stops playback."""
        if not self.static_playlist or not (0 <= row < len(self.songs_model)):
            return
        station = self.songs_model.song_at(row)
        if row == self.current_song_index:
            self.stop()
            self.current_song_index = None
        elif self.current_song_index is not None and row < self.current_song_index:
            self.current_song_index -= 1
        self.songs_model.remove_row(row)
        self._reindex_songs()
        # Removing a radio row also drops it from the saved favourites.
        if self.source == SOURCE_RADIO and isinstance(station, radio.RadioStation):
            favorites = [s for s in radio.load_favorites()
                         if s.audioUrl != station.audioUrl]
            radio.save_favorites(favorites)

    def remove_songs(self, rows):
        """Remove several rows (local playback). Descending order keeps indices
        valid as each is removed."""
        for r in sorted(set(rows), reverse=True):
            self.remove_song(r)

    def clear_playlist(self):
        """Empty a static playlist (local files / radio favourites) and stop."""
        if not self.static_playlist:
            return
        self.stop()
        self.current_song_index = None
        self.songs_model.clear()

    def sort_songs(self, key):
        """Reorder the local playlist by 'title', 'artist', 'reverse' or
        'random', keeping the current song current."""
        if not self.local_mode or len(self.songs_model) < 2:
            return
        cur_song = self.current_song
        n = len(self.songs_model)
        songs = [self.songs_model.song_at(i) for i in range(n)]
        order = list(range(n))
        if key == 'title':
            order.sort(key=lambda i: (songs[i].title or '').lower())
        elif key == 'artist':
            order.sort(key=lambda i: (songs[i].artist or '').lower())
        elif key == 'reverse':
            order.reverse()
        elif key == 'random':
            random.shuffle(order)
        self.songs_model.reorder(order)
        self._reindex_songs()
        if cur_song is not None:
            for r in range(len(self.songs_model)):
                if self.songs_model.song_at(r) is cur_song:
                    self.current_song_index = r
                    break

    def move_songs(self, rows, target):
        """Move ``rows`` so they land at insertion index ``target`` (before the
        original row at that index). Static playlists only (local files / radio
        favourites); keeps the current song current. Returns the moved block's
        new start row, or None on no-op."""
        if not self.static_playlist:
            return None
        n = len(self.songs_model)
        moving = sorted(r for r in set(rows) if 0 <= r < n)
        if not moving:
            return None
        cur_song = self.current_song
        remaining = [i for i in range(n) if i not in set(moving)]
        before = sum(1 for i in remaining if i < target)     # non-moving rows above the drop
        new_order = remaining[:before] + moving + remaining[before:]
        if new_order == list(range(n)):
            return None
        self.songs_model.reorder(new_order)
        self._reindex_songs()
        if cur_song is not None:
            for r in range(len(self.songs_model)):
                if self.songs_model.song_at(r) is cur_song:
                    self.current_song_index = r
                    break
        if self.source == SOURCE_RADIO:
            self._persist_radio_order()
        return before

    def _persist_radio_order(self):
        """Save the current radio row order back to the favourites file, keeping
        only rows that are saved favourites (a standalone tuned-in station that
        isn't a favourite is left out)."""
        fav_urls = {s.audioUrl for s in radio.load_favorites()}
        ordered = [s for i in range(len(self.songs_model))
                   if isinstance(s := self.songs_model.song_at(i), radio.RadioStation)
                   and s.audioUrl in fav_urls]
        radio.save_favorites(ordered)

    def _reindex_songs(self):
        for r in range(len(self.songs_model)):     # keep song.index == its row
            self.songs_model.song_at(r).index = r

    def _is_skipped(self, idx):
        song = self.songs_model.song_at(idx) if idx is not None else None
        return getattr(song, 'skip', False)

    def next_song(self, *ignore):
        if self.current_song_index is None:
            return
        n = len(self.songs_model)
        if self.static_playlist:
            idx = self._next_local_index(+1)
            for _ in range(n):                 # advance past skip-marked songs
                if idx is None or not self._is_skipped(idx):
                    break
                idx = self._next_local_index(+1, from_idx=idx)
            return self.stop() if idx is None else self.start_song(idx)
        # Pandora queue: jump to the next song not marked to skip, dropping the
        # skipped songs from the list as we pass them (the queue buffer is kept
        # full via _ensure_queue_buffer on song_changed/songs_added).
        idx = self.current_song_index + 1
        skipped = []
        while idx < n and self._is_skipped(idx):
            skipped.append(idx)
            idx += 1
        if skipped:
            for r in reversed(skipped):        # descending keeps indices valid
                self.songs_model.remove_row(r)
            idx -= len(skipped)                # target shifted down by removals
            self._reindex_songs()
        self.start_song(idx)

    def set_songs_skip(self, rows, skip):
        """Mark/unmark songs so playback jumps over them when advancing (a
        preemptive skip for queued songs)."""
        for r in rows:
            song = self.songs_model.song_at(r)
            if song is not None:
                song.skip = bool(skip)

    def prev_song(self, *ignore):
        # Only static playlists can go backwards; a Pandora stream can't be
        # rewound (radio favourites can, moving between stations).
        if not self.static_playlist or self.current_song_index is None:
            return
        idx = self._next_local_index(-1)
        if idx is not None:
            self.start_song(idx)

    def _build_shuffle_order(self, first=None):
        """A random permutation of the playlist indices. If ``first`` is a valid
        index it's placed at position 0, so playback continues from the current
        song and cycles through the rest before wrapping."""
        order = list(range(len(self.songs_model)))
        random.shuffle(order)
        if first is not None and first in order and order[0] != first:
            order.remove(first)
            order.insert(0, first)
        self._shuffle_order = order
        return order

    def _next_local_index(self, step, from_idx=None):
        """The next playlist index for local playback given shuffle/repeat, or
        None when the playlist ends and repeat is off. ``from_idx`` overrides the
        reference position (used to advance past skip-marked songs)."""
        n = len(self.songs_model)
        if n == 0:
            return None
        cur = from_idx if from_idx is not None else \
            (self.current_song_index if self.current_song_index is not None else 0)
        if self.shuffle:
            order = self._shuffle_order
            if not order or len(order) != n:
                order = self._build_shuffle_order(first=cur)
            try:
                pos = order.index(cur)
            except ValueError:
                pos = 0
            pos += step
            if 0 <= pos < n:
                return order[pos]
            if not self.repeat:
                return None
            # Wrap: reshuffle for the new cycle, avoiding an immediate repeat.
            order = self._build_shuffle_order()
            end = 0 if step > 0 else n - 1
            if n > 1 and order[end] == cur:
                other = 1 if step > 0 else n - 2
                order[end], order[other] = order[other], order[end]
            return order[end]
        idx = cur + step
        if 0 <= idx < n:
            return idx
        if not self.repeat:
            return None
        return idx % n

    def set_shuffle(self, on):
        self.shuffle = bool(on)
        self.settings['shuffle'] = self.shuffle
        self._shuffle_order = None

    def set_repeat(self, on):
        self.repeat = bool(on)
        self.settings['repeat'] = self.repeat

    def toggle_shuffle(self, *ignore):
        self.set_shuffle(not self.shuffle)

    def toggle_repeat(self, *ignore):
        self.set_repeat(not self.repeat)

    # ------------------------------------------------------- sleep timer
    def set_sleep(self, minutes):
        """Stop playback after ``minutes`` (0 = off). Replaces any end-of-track
        sleep. The classic Winamp Sleep function."""
        self._sleep_end_of_track = False
        self._sleep_minutes = int(minutes) if minutes else 0
        if self._sleep_timer is None:
            self._sleep_timer = QTimer(self)
            self._sleep_timer.setSingleShot(True)
            self._sleep_timer.timeout.connect(self._on_sleep_elapsed)
        self._sleep_timer.stop()
        if self._sleep_minutes > 0:
            self._sleep_deadline = time.monotonic() + self._sleep_minutes * 60
            self._sleep_timer.start(self._sleep_minutes * 60 * 1000)
        else:
            self._sleep_deadline = None

    def set_sleep_end_of_track(self, *ignore):
        """Stop playback when the current track finishes."""
        if self._sleep_timer is not None:
            self._sleep_timer.stop()
        self._sleep_deadline = None
        self._sleep_minutes = 0
        self._sleep_end_of_track = True

    def _on_sleep_elapsed(self):
        self._sleep_deadline = None
        self._sleep_minutes = 0
        self.stop()

    def sleep_status(self):
        """(mode, remaining_secs, minutes) for the UI. mode is
        'off' | 'timer' | 'track'."""
        if self._sleep_end_of_track:
            return ('track', None, 0)
        if self._sleep_deadline is not None:
            return ('timer', max(0, int(self._sleep_deadline - time.monotonic())),
                    self._sleep_minutes)
        return ('off', None, 0)

    # ----------------------------------------------------- local playback
    def open_local_files(self, *ignore):
        exts = ' '.join('*' + e for e in local.AUDIO_EXTENSIONS)
        paths, _sel = QFileDialog.getOpenFileNames(
            self, _('Open Audio Files'), self._local_dir,
            '{} ({})'.format(_('Audio files'), exts))
        if not paths:
            return
        self._local_dir = os.path.dirname(paths[0])
        self._load_local(paths, os.path.basename(self._local_dir) or _('Local Files'))

    def open_local_folder(self, *ignore):
        d = QFileDialog.getExistingDirectory(self, _('Open Folder'), self._local_dir)
        if not d:
            return
        self._local_dir = d
        self._load_local([d], os.path.basename(d.rstrip('/')) or _('Local Files'))

    def open_playlist(self, *ignore):
        exts = ' '.join('*' + e for e in local.PLAYLIST_EXTENSIONS)
        path, _sel = QFileDialog.getOpenFileName(
            self, _('Open Playlist'), self._local_dir,
            '{} ({})'.format(_('Playlists'), exts))
        if not path:
            return
        self._local_dir = os.path.dirname(path)
        paths = local.read_playlist(path)
        if not paths:
            QMessageBox.information(self, _('Open Playlist'),
                                    _('The playlist has no playable files.'))
            return
        self._load_local(paths, os.path.splitext(os.path.basename(path))[0])

    def save_playlist(self, *ignore):
        songs = [self.songs_model.song_at(i) for i in range(len(self.songs_model))]
        if not songs:
            QMessageBox.information(self, _('Save Playlist'),
                                    _('The playlist is empty.'))
            return
        start = os.path.join(self._local_dir or os.path.expanduser('~'),
                             '{}.m3u'.format(self.stations_button.text() or 'playlist'))
        path, _sel = QFileDialog.getSaveFileName(
            self, _('Save Playlist'), start,
            '{} (*.m3u)'.format(_('M3U playlist')))
        if not path:
            return
        if os.path.splitext(path)[1].lower() not in ('.m3u', '.m3u8'):
            path += '.m3u'
        try:
            local.write_m3u(path, songs)
            self._local_dir = os.path.dirname(path)
        except OSError as e:
            QMessageBox.warning(self, _('Save Playlist'),
                                _('Could not save:\n{}').format(e))

    def add_local_files(self, paths):
        """Load dropped audio files/folders: enqueue onto the current local
        playlist when already in local playback, otherwise switch to local and
        play them (classic Winamp drop-to-add)."""
        if not paths:
            return
        first = paths[0]
        if os.path.isdir(first):
            label = os.path.basename(first.rstrip('/')) or _('Local Files')
        else:
            label = os.path.basename(os.path.dirname(first)) or _('Local Files')
        self._load_local(paths, label, append=self.local_mode)

    def _load_local(self, paths, label, append=False):
        files = local.collect_audio_files(paths)
        if not files:
            QMessageBox.information(self, _('Open'),
                                    _('No playable audio files were found.'))
            return
        self.status_push('net', _('Reading files…'))
        songs = local.build_songs(files)
        self.status_pop('net')
        if not songs:
            return

        if append and self.local_mode:
            # Enqueue onto the existing local playlist without disrupting play.
            was_empty = len(self.songs_model) == 0
            first_new = len(self.songs_model)
            for s in songs:
                s.index = len(self.songs_model)
                self.songs_model.append_song(s)
                self.update_song_row(s)
                self._set_local_art(s)
            if was_empty or self.current_song_index is None:
                self.start_song(first_new)
            return

        self._snapshot_pandora()   # keep the Pandora queue to resume later
        self.stop()
        self._set_source(SOURCE_LOCAL)
        self.current_station = None
        self.current_song_index = None
        self.songs_model.clear()
        self._shuffle_order = None
        for s in songs:
            s.index = len(self.songs_model)
            self.songs_model.append_song(s)
            self.update_song_row(s)
            self._set_local_art(s)
        self.stations_button.setText(label)
        self.start_song(0)

    def _set_local_art(self, song):
        """Turn a LocalSong's embedded/sidecar art bytes into a row pixmap."""
        if not song.art_bytes:
            return
        img = QImage.fromData(song.art_bytes)
        if img.isNull():
            return
        pixmap = QPixmap.fromImage(img.scaled(
            ALBUM_ART_SIZE, ALBUM_ART_SIZE,
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
        song.art_pixbuf = pixmap
        self.songs_model.update_row(song.index, pixmap=pixmap)

    def switch_to_local(self):
        """Enter Local Playback mode with an empty playlist; the user then opens
        files/folders. No-op if already in local mode."""
        if self.local_mode:
            return
        self._snapshot_pandora()   # keep the Pandora queue to resume later
        self._set_source(SOURCE_LOCAL)
        self.stop()
        self.current_station = None
        self.current_song_index = None
        self.songs_model.clear()
        self.stations_button.setText(_('Local Playback'))
        self.setWindowTitle('Pyrrha')

    def switch_to_pandora(self):
        """Leave Local/Radio playback and return to the last Pandora station."""
        if self.source == SOURCE_PANDORA:
            return
        self.source = SOURCE_PANDORA
        self.stop()
        self.current_song_index = None
        self.songs_model.clear()
        station = self._find_station(self.current_station_id)
        if station is None and self.stations_model:
            station = self.stations_model[0][0]
        if station is not None:
            self.station_changed(station)   # current_station is None, so this runs
        else:
            self.show_stations()

    def tune_radio(self, station):
        """Tune in an internet-radio station: switch to the radio source, show
        the saved favourites as the song list (with this station included), and
        start streaming the chosen one. The list rows are RadioStation objects,
        so next/prev walks between favourites and ICY tags update the row in
        place (see on_gst_tag)."""
        self._snapshot_pandora()   # keep the Pandora queue to resume later
        self.stop()
        self._set_source(SOURCE_RADIO)
        self.current_station = None
        self.current_song_index = None
        self._shuffle_order = None
        self.songs_model.clear()

        stations = radio.load_favorites()
        idx = next((i for i, s in enumerate(stations)
                    if s.audioUrl == station.audioUrl), None)
        if idx is None:                 # not a saved favourite: play it standalone
            stations.insert(0, station)
            idx = 0
        for s in stations:
            s.index = len(self.songs_model)
            self.songs_model.append_song(s)
            self.update_song_row(s)
            self._fetch_row_art(s)

        self.stations_button.setText(_('Internet Radio'))
        self.setWindowTitle('Pyrrha')
        self.start_song(idx)

        if station.stationuuid:         # popularity ping, best-effort
            self.worker_run(radiobrowser.click,
                            (station.stationuuid, self.get_proxy()),
                            None, None, context=None)

    def _fetch_row_art(self, song):
        """Download a song/station's ``artRadio`` image on the worker thread and
        set it as the row pixmap. Used for radio-station logos and for restored
        Pandora rows (whose art wasn't serialised). Best-effort; keyed off the
        stable trackToken."""
        if not getattr(song, 'artRadio', None):
            return

        def fetch(url, tmpdir, station, index):
            try:
                with urllib.request.urlopen(url, timeout=10) as f:
                    image = f.read()
            except (urllib.error.URLError, OSError) as e:
                logging.info('Could not fetch station logo %s: %s', url, e)
                return (None, None, station, index)
            file_url = None
            if tmpdir:
                try:
                    self.clear_art_cache()
                    name = hashlib.sha256(
                        station.trackToken.encode('utf-8')).hexdigest() + '.img'
                    cache_file_path = os.path.join(self.tempdir, name)
                    file_url = urllib.parse.urljoin(
                        'file://', urllib.parse.quote(cache_file_path))
                    if not os.path.exists(cache_file_path):
                        with open(cache_file_path, 'xb') as fh:
                            fh.write(image)
                except IOError:
                    logging.warning('Failed to write station-art tempfile')
            return (image, file_url, station, index)

        def done(t):
            image, file_url, station, index = t
            # Guard against the list having changed since the fetch started.
            if not (0 <= index < len(self.songs_model)
                    and self.songs_model.song_at(index) is station):
                return
            if image is None:
                return
            img = QImage.fromData(image)
            if img.isNull():
                return
            pixmap = QPixmap.fromImage(img.scaled(
                ALBUM_ART_SIZE, ALBUM_ART_SIZE,
                Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            station.art_pixbuf = pixmap
            self.songs_model.update_row(index, pixmap=pixmap)
            self.update_song_row(station)
            if file_url:
                station.artUrl = file_url
                if station is self.current_song:
                    self.metadata_changed.emit(station)

        self.worker_run(fetch, (song.artRadio, self.tempdir, song, song.index),
                        done, None, context=None)

    def _resolve_and_start(self, song_index, song, jump):
        """Resolve a playlist-wrapped radio URL on a worker, then start it. The
        resolved URL is cached on the station, so subsequent plays skip this."""
        def still_current():
            return (self.source == SOURCE_RADIO
                    and 0 <= song_index < len(self.songs_model)
                    and self.songs_model.song_at(song_index) is song)

        def resolved(url):
            if not still_current():
                return
            if url and url != song.audioUrl:
                song.audioUrl = url
            self.start_song(song_index, jump=jump)

        def failed(error):
            self.status_pop('net')
            logging.info('Stream resolve failed: %s', getattr(error, 'traceback', error))
            if still_current():
                self.start_song(song_index, jump=jump)   # fall back to the raw URL

        self.worker_run(radio.resolve_stream_url, (song.audioUrl, self.get_proxy()),
                        resolved, _('Resolving stream…'), errorback=failed)

    # ------------------------------------------------------- radio favourites
    def is_radio_favorite(self, station):
        return any(s.audioUrl == station.audioUrl
                   for s in radio.load_favorites())

    def add_radio_favorite(self, station):
        """Persist a station to the favourites file and, when already in radio
        mode, append it as a new row (with its logo)."""
        favorites = radio.load_favorites()
        if any(s.audioUrl == station.audioUrl for s in favorites):
            return
        favorites.append(station)
        radio.save_favorites(favorites)
        if self.source == SOURCE_RADIO and not any(
                self.songs_model.song_at(i).audioUrl == station.audioUrl
                for i in range(len(self.songs_model))):
            station.index = len(self.songs_model)
            self.songs_model.append_song(station)
            self.update_song_row(station)
            self._fetch_row_art(station)

    def remove_radio_favorite(self, station):
        """Drop a station from the favourites file and, when in radio mode,
        remove its row."""
        favorites = [s for s in radio.load_favorites()
                     if s.audioUrl != station.audioUrl]
        radio.save_favorites(favorites)
        if self.source == SOURCE_RADIO:
            for i in range(len(self.songs_model)):
                if self.songs_model.song_at(i).audioUrl == station.audioUrl:
                    self.remove_song(i)
                    break

    def _find_station(self, station_id):
        if station_id is None:
            return None
        for row in self.stations_model:
            if row[0].id == station_id:
                return row[0]
        return None

    # -------------------------------------------------- Pandora queue persistence
    def _snapshot_pandora(self):
        """Capture the current Pandora queue into ``_pandora_saved`` so it can be
        restored later. No-op unless we're actually on a Pandora station."""
        if self.source != SOURCE_PANDORA or self.current_station is None:
            return
        songs = [self.songs_model.song_at(i) for i in range(len(self.songs_model))]
        songs = [s for s in songs if s is not None]
        if not songs:
            return
        self._pandora_saved = {
            'station_id': self.current_station_id,
            'songs': songs,
            'index': self.current_song_index,
        }

    def _restore_pandora_queue(self, station):
        """Repopulate the song list for ``station`` from the saved queue, pruning
        expired songs, and cue (but do NOT play) the song we left off on. Returns
        True if a usable queue was restored; False to fall back to fetching."""
        saved, self._pandora_saved = self._pandora_saved, None   # consume
        if not saved or saved.get('station_id') != station.id:
            return False
        all_songs = saved.get('songs') or []
        songs = [s for s in all_songs if _song_still_valid(s)]
        if not songs:
            return False

        self.songs_model.clear()
        for i, s in enumerate(songs):
            s.index = i
            s.start_time = s.position = None
            s.finished = False
            self.songs_model.append_song(s)
            self.update_song_row(s)
            if getattr(s, 'art_pixbuf', None) is None:
                self._fetch_row_art(s)

        # Keep pointing at the song we left off on if it survived pruning.
        old_index = saved.get('index')
        target = all_songs[old_index] if (old_index is not None
                                          and 0 <= old_index < len(all_songs)) else None
        self.current_song_index = songs.index(target) if target in songs else 0

        cur = songs[self.current_song_index]
        idx = self.songs_model.index(self.current_song_index)
        self.songs_view.setCurrentIndex(idx)
        self.songs_view.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        self.setWindowTitle("%s by %s - Pyrrha" % (cur.title, cur.artist))
        # Cue paused: update MPRIS/UI metadata but do NOT emit song_changed (which
        # would trigger scrobbling and a queue-buffer fetch) — playback is stopped
        # until the user hits play (see play() resume-from-stopped).
        self.metadata_changed.emit(cur)
        logging.info('Restored Pandora queue: %d songs (of %d), at index %d',
                     len(songs), len(all_songs), self.current_song_index)
        return True

    def _prune_expired_songs(self):
        """Drop upcoming Pandora songs whose audio URLs have expired, and top the
        queue back up if that left it short. Runs on a periodic timer and before
        playing. Only touches songs *after* the current one."""
        if self.source != SOURCE_PANDORA:
            return 0
        cur = self.current_song_index if self.current_song_index is not None else -1
        removed = 0
        for r in range(len(self.songs_model) - 1, cur, -1):
            s = self.songs_model.song_at(r)
            if s is not None and not _song_still_valid(s):
                self.songs_model.remove_row(r)
                removed += 1
        if removed:
            self._reindex_songs()
            self._ensure_queue_buffer()
        return removed

    def _on_prune_timer(self):
        self._prune_expired_songs()
        return True   # keep the GLib timeout running

    # -- disk persistence --
    def _pandora_queue_file(self):
        d = os.path.join(GLib.get_user_config_dir(), 'pyrrha')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, 'pandora_queue.json')

    def _save_pandora_queue(self):
        """Write the saved Pandora queue to disk (called on quit). Snapshots the
        live queue first if we're currently on Pandora."""
        self._snapshot_pandora()
        saved = self._pandora_saved
        songs = (saved or {}).get('songs') or []
        try:
            path = self._pandora_queue_file()
            states = [s.to_state() for s in songs if hasattr(s, 'to_state')]
            if not states:
                if os.path.exists(path):
                    os.remove(path)
                return
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'station_id': saved['station_id'],
                           'index': saved.get('index'),
                           'saved_at': time.time(), 'songs': states}, f)
        except (OSError, ValueError, KeyError) as e:
            logging.warning('Could not save Pandora queue: %s', e)

    def _load_pandora_queue_from_disk(self):
        """Load a previously saved Pandora queue into ``_pandora_saved`` (pruning
        expired songs). Requires a connected ``pandora`` for song rebuild."""
        try:
            with open(self._pandora_queue_file(), encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logging.warning('Could not read Pandora queue: %s', e)
            return
        songs = []
        for st in data.get('songs', []):
            try:
                song = Song.from_state(self.pandora, st)
            except Exception as e:
                logging.info('Skipping unrestorable Pandora song: %s', e)
                continue
            if _song_still_valid(song):
                songs.append(song)
        if songs:
            self._pandora_saved = {'station_id': data.get('station_id'),
                                   'songs': songs, 'index': data.get('index')}
            logging.info('Loaded %d valid Pandora songs from disk', len(songs))

    # --------------------------------------------------------- state machine
    def _set_player_state(self, target, change_gst_state=False):
        change_gst_state = change_gst_state or self._current_state is not PseudoGst.BUFFERING
        if change_gst_state:
            ret = self.player.set_state(target.state)
            if ret == Gst.StateChangeReturn.FAILURE:
                logging.warning('Error changing player state from: {} to: {}'.format(
                    self._current_state, target))
                return False
            self._current_state = target
            if self._current_state is PseudoGst.PLAYING:
                self.create_ui_loop()
            else:
                self.destroy_ui_loop()
        if target is not PseudoGst.BUFFERING:
            self._buffer_recovery_state = target
        self.update_song_row()
        return True

    def user_play(self, *ignore):
        if self.play():
            self.user_changed_play_state.emit(True)

    def play(self, change_gst_state=False):
        if self.current_song is None:
            return False
        if not self.current_song.is_still_valid():
            self.current_song.message = 'Song expired'
            self.update_song_row()
            return self.next_song()
        if self._current_state is PseudoGst.STOPPED:
            # Cued but never started (e.g. a restored queue): start it now.
            self.start_song(self.current_song_index
                            if self.current_song_index is not None else 0)
            return True
        if self._set_player_state(PseudoGst.PLAYING, change_gst_state=change_gst_state):
            self.playpause_button.setIcon(self._pause_icon())
            self.play_state_changed.emit(True)
        return True

    def user_pause(self, *ignore):
        self.pause()
        self.user_changed_play_state.emit(False)

    def pause(self):
        if self._set_player_state(PseudoGst.PAUSED):
            self.playpause_button.setIcon(self._play_icon())
            self.play_state_changed.emit(False)

    def stop(self):
        prev = self.current_song
        if prev and getattr(prev, 'start_time', None):
            prev.finished = True
            prev.position = self.query_position()
            self.song_ended.emit(prev)
        if self._set_player_state(PseudoGst.STOPPED, change_gst_state=True):
            self.playpause_button.setIcon(self._pause_icon())

    def user_playpause(self, *ignore):
        if self._current_state is PseudoGst.STOPPED:
            self.user_play()          # cued/stopped queue → start it
        elif self.playing:
            self.user_pause()
        else:
            self.user_play()

    def playpause(self, *ignore):
        # Plain toggle without the user_* semantics (used by MPRIS PlayPause).
        if self._current_state is PseudoGst.STOPPED:
            self.play()
        elif self.playing:
            self.pause()
        else:
            self.play()

    def playpause_notify(self, *ignore):
        # Toggle as if the user pressed play/pause (used by media keys).
        self.user_playpause()

    def bring_to_top(self, *ignore):
        self.show()
        self.raise_()
        self.activateWindow()

    def quit(self, *ignore):
        # An explicit quit (tray menu, MPRIS) bypasses close interceptors.
        self._save_pandora_queue()
        self.stop()
        self.app.quit()

    # ------------------------------------------------------------- playlist
    def clear_art_cache(self):
        timestamp = time.time()
        with os.scandir(self.tempdir) as art_list:
            for art in art_list:
                age = timestamp - art.stat().st_mtime
                if age > ART_CACHE_TIME:
                    os.remove(os.path.join(self.tempdir, art.path))

    def fetch_more_songs(self):
        """Proactively pull another batch of Pandora songs into the queue without
        interrupting playback — get_playlist() runs on a worker and only appends
        (start=False). No-op in local mode or while a fetch is already running."""
        if not self.local_mode and self.current_station is not None \
                and not self.waiting_for_playlist:
            self.get_playlist()

    def _unskipped_ahead(self):
        cur = self.current_song_index
        if cur is None:
            return len(self.songs_model)
        return sum(1 for j in range(cur + 1, len(self.songs_model))
                   if not self._is_skipped(j))

    def _ensure_queue_buffer(self):
        """Keep PREQUEUE_TARGET unskipped songs queued ahead of the current one
        (Pandora), fetching another batch in the background if we're short."""
        if self.local_mode or self.current_station is None:
            return
        target = self.settings['prequeue-size']
        # 0 (Off): don't over-fetch; start_song() still pulls the next batch as
        # the queue is exhausted, so playback never stalls.
        if target > 0 and self._unskipped_ahead() < target:
            self.fetch_more_songs()

    def _on_songs_added(self, count):
        # Chain fetches until the buffer is full; stop if a fetch returned nothing.
        if count > 0:
            self._ensure_queue_buffer()

    def get_playlist(self, start=False):
        if self.local_mode or self.current_station is None:
            return   # only a Pandora station is fetched; local/radio are static
        if self.playlist_update_timer_id:
            GLib.source_remove(self.playlist_update_timer_id)
        self.playlist_update_timer_id = 0
        songs_left_to_process = 0
        song_count = 0
        self.start_new_playlist = self.start_new_playlist or start
        if self.waiting_for_playlist:
            return

        if self.gstreamer_errorcount_1 >= self.playcount and self.gstreamer_errorcount_2 >= 1:
            logging.warning("Too many gstreamer errors. Not retrying")
            self.waiting_for_playlist = 1
            self.error_dialog(self.gstreamer_error, self.get_playlist)
            return

        def emit_songs_added(song_count):
            self.playlist_update_timer_id = 0
            self.songs_added.emit(song_count)
            return False

        def get_album_art(url, tmpdir, *extra):
            try:
                with urllib.request.urlopen(url) as f:
                    image = f.read()
            except urllib.error.HTTPError:
                logging.warning('Invalid image url received')
                return (None, None,) + extra

            file_url = None
            song, index = extra
            if tmpdir:
                try:
                    self.clear_art_cache()
                    filename_hash = hashlib.sha256(
                        (song.artist + song.album).encode('utf-8')).hexdigest() + '.jpeg'
                    cache_file_path = os.path.join(self.tempdir, filename_hash)
                    file_url = urllib.parse.urljoin('file://', urllib.parse.quote(cache_file_path))
                    if not os.path.exists(cache_file_path):
                        with open(cache_file_path, 'xb') as f:
                            f.write(image)
                except IOError:
                    logging.warning("Failed to write art tempfile")
            return (image, file_url,) + extra

        def art_callback(t):
            nonlocal songs_left_to_process
            image, file_url, song, index = t
            songs_left_to_process -= 1
            if index < len(self.songs_model) and self.songs_model.song_at(index) is song:
                pixmap = None
                if image is not None:
                    img = QImage.fromData(image)
                    if not img.isNull():
                        pixmap = QPixmap.fromImage(img.scaled(
                            ALBUM_ART_SIZE, ALBUM_ART_SIZE,
                            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                song.art_pixbuf = pixmap
                self.songs_model.update_row(index, pixmap=pixmap)
                self.update_song_row(song)
                if file_url:
                    song.artUrl = file_url
                    if song is self.current_song or not self.playlist_update_timer_id:
                        self.metadata_changed.emit(song)
                if not songs_left_to_process and self.playlist_update_timer_id:
                    GLib.source_remove(self.playlist_update_timer_id)
                    emit_songs_added(song_count)

        def callback(songs):
            nonlocal songs_left_to_process, song_count
            songs_left_to_process = song_count = len(songs)
            start_index = len(self.songs_model)
            for i in songs:
                i.index = len(self.songs_model)
                i.art_pixbuf = None
                self.songs_model.append_song(i)
                self.update_song_row(i)
                if i.artRadio:
                    self.worker_run(get_album_art, (i.artRadio, self.tempdir, i, i.index),
                                    art_callback)
                else:
                    songs_left_to_process -= 1
            self.playlist_update_timer_id = GLib.timeout_add_seconds(
                song_count, emit_songs_added, song_count)

            self.status_pop('net')
            if self.start_new_playlist:
                self.start_song(start_index)

            self.gstreamer_errorcount_2 = self.gstreamer_errorcount_1
            self.gstreamer_errorcount_1 = 0
            self.playcount = 0
            self.waiting_for_playlist = False
            self.start_new_playlist = False

        self.waiting_for_playlist = True
        self.worker_run(self.current_station.get_playlist, (), callback, "Getting songs...")

    # ------------------------------------------------------------- gst bus
    def query_position(self):
        if self.player.query(self._query_position):
            return self._query_position.parse_position()[1]

    def query_duration(self):
        if self.player.query(self._query_duration):
            return self._query_duration.parse_duration()[1]

    def seekable(self):
        """Only local files can be seeked; Pandora streams cannot."""
        return self.local_mode

    def seek(self, position_ns):
        """Seek the player to an absolute position (nanoseconds)."""
        if not self.seekable():
            return
        dur = self.query_duration()
        position_ns = int(max(0, position_ns))
        if dur:
            position_ns = min(position_ns, dur)
        self.player.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, position_ns)
        # Reflect the new position immediately in the song row / skin.
        if self.current_song is not None:
            self.current_song.position = position_ns
        self.update_song_row()

    def query_buffer(self):
        if self.player.query(self._query_buffer):
            return self._query_buffer.parse_buffering_percent()[0]
        return True

    def on_gst_stream_start(self, bus, message):
        if self.current_song is None:
            return
        self.current_song.duration = self.query_duration() or self.current_song.trackLength * Gst.SECOND
        self.current_song.duration_message = self.format_time(self.current_song.duration)
        self.update_song_row()
        if self.current_song.get_duration_sec() != self.current_song.trackLength:
            self.metadata_changed.emit(self.current_song)

    def on_gst_tag(self, bus, message):
        """ICY now-playing: Icecast/Shoutcast push the current track as a tag
        message carrying GST_TAG_TITLE ("Artist - Title"). Apply it to the
        station row so the list, window title, and every metadata consumer
        (MPRIS, tray, notifications, Last.fm scrobbling) update live."""
        if self.source != SOURCE_RADIO or self.current_song is None:
            return
        taglist = message.parse_tag()
        ok, streamtitle = taglist.get_string(Gst.TAG_TITLE)
        if not ok or not streamtitle:
            return
        if not self.current_song.set_stream_title(streamtitle):
            return
        song = self.current_song
        os.environ['PULSE_PROP_media.title'] = song.title
        os.environ['PULSE_PROP_media.artist'] = song.artist
        self.setWindowTitle("%s by %s - Pyrrha" % (song.title, song.artist))
        self.update_song_row()
        self.metadata_changed.emit(song)

    def on_gst_eos(self, bus, message):
        logging.info("EOS")
        if self._sleep_end_of_track:
            self._sleep_end_of_track = False
            self.stop()
            return
        if self.source == SOURCE_RADIO:
            # A live stream shouldn't end; EOS means the connection dropped.
            # Retry the same station a few times before advancing/stopping.
            self._radio_reconnect()
            return
        self.next_song()

    def _radio_reconnect(self):
        song = self.current_song
        if song is None:
            return
        if self._radio_reconnect_left <= 0:
            logging.info("Radio stream '%s' ended; giving up reconnect.", song.name)
            song.message = _('Stream unavailable')
            self.update_song_row()
            return self.stop()
        self._radio_reconnect_left -= 1
        logging.info("Radio stream '%s' dropped; reconnecting (%d left).",
                     song.name, self._radio_reconnect_left)
        self.stop()
        self.player.set_property("uri", song.audioUrl)
        self._set_player_state(PseudoGst.BUFFERING)

    def on_gst_plugin_installed(self, result, userdata):
        if result == GstPbutils.InstallPluginsReturn.SUCCESS:
            self.fatal_error_dialog(
                _("Codec installation successful"),
                _("The required codec was installed, please restart Pyrrha."))
        else:
            self.error_dialog(
                _("Codec installation failed"), None,
                submsg=_("The required codec failed to install. Either manually install "
                         "it or try another quality setting."))

    def on_gst_element(self, bus, message):
        struct = message.get_structure()
        if struct is not None and struct.get_name() == 'spectrum':
            self._update_spectrum(struct)
            return
        if GstPbutils.is_missing_plugin_message(message):
            if GstPbutils.install_plugins_supported():
                details = GstPbutils.missing_plugin_message_get_installer_detail(message)
                GstPbutils.install_plugins_async([details], None, self.on_gst_plugin_installed, None)
            else:
                self.error_dialog(
                    _("Missing codec"), None,
                    submsg=_("GStreamer is missing a plugin and it could not be automatically "
                             "installed. Either manually install it or try another quality setting."))

    def _update_spectrum(self, struct):
        # PyGObject can't return the GstValueList directly, so parse the
        # structure's string form (magnitudes in dB) and normalize to 0..1.
        m = _SPECTRUM_RE.search(struct.to_string())
        if not m:
            return
        floor = float(SPECTRUM_THRESHOLD)
        bands = []
        for tok in m.group(1).split(','):
            try:
                db = float(tok)
            except ValueError:
                continue
            bands.append(min(1.0, max(0.0, (db - floor) / -floor)))
        if bands:
            self.spectrum_bands = bands

    # --------------------------------------------------- per-station EQ
    def _station_eq_file(self):
        d = os.path.join(GLib.get_user_config_dir(), 'pyrrha')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, 'station_eq.json')

    def _load_station_eq(self):
        try:
            with open(self._station_eq_file()) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (IOError, ValueError):
            return {}

    def eq_profile_key(self):
        """Storage key for the current mode's remembered EQ: per-station under
        Pandora, and a single shared curve for each of Local and Radio. None when
        Pandora has no station yet (nothing to key on)."""
        if self.source == SOURCE_LOCAL:
            return '__local__'
        if self.source == SOURCE_RADIO:
            return '__radio__'
        sid = self.current_station_id
        return str(sid) if sid else None

    def get_eq_profile(self):
        """Saved EQ ({'bands': [...], 'preamp': float, 'on': bool}) for the
        current mode/station, or None."""
        key = self.eq_profile_key()
        return self._station_eq.get(key) if key is not None else None

    def set_eq_profile(self, bands, preamp, on):
        """Remember the EQ curve for the current mode/station (persisted)."""
        key = self.eq_profile_key()
        if key is None:
            return
        self._station_eq[key] = {
            'bands': [float(b) for b in bands], 'preamp': float(preamp),
            'on': bool(on)}
        try:
            with open(self._station_eq_file(), 'w') as f:
                json.dump(self._station_eq, f)
        except IOError:
            logging.warning('Failed to save the EQ profile')

    def audio_stream_info(self):
        """(bitrate_kbps, sample_rate_hz, channels) for the current stream; any
        field may be None. Drives the skinned kbps/kHz/stereo indicators."""
        bitrate = None
        song = self.current_song
        if song is not None and getattr(song, 'bitrate', None) is not None:
            try:
                bitrate = int(song.bitrate)
            except (TypeError, ValueError):
                bitrate = None
        rate = channels = None
        try:
            pad = self.rgvolume.get_static_pad('sink')
            caps = pad.get_current_caps() if pad is not None else None
            if caps is not None and caps.get_size() > 0:
                s = caps.get_structure(0)
                ok, rate = s.get_int('rate')
                rate = rate if ok else None
                ok, channels = s.get_int('channels')
                channels = channels if ok else None
        except Exception:
            pass
        return bitrate, rate, channels

    # --------------------------------------------------- skin selection
    def _last_skin_file(self):
        d = os.path.join(GLib.get_user_config_dir(), 'pyrrha')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, 'skin.txt')

    def get_last_skin(self):
        try:
            with open(self._last_skin_file()) as f:
                return f.read().strip() or None
        except IOError:
            return None

    def set_last_skin(self, path):
        try:
            with open(self._last_skin_file(), 'w') as f:
                f.write(path)
        except IOError:
            logging.warning('Failed to save the last skin path')

    def _classic_layout_file(self):
        d = os.path.join(GLib.get_user_config_dir(), 'pyrrha')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, 'skin_layout.json')

    def get_classic_layout(self):
        """Saved tear-off offsets for the classic panels: {role: [x, y]} in
        logical pixels (empty on first run)."""
        try:
            with open(self._classic_layout_file()) as f:
                return json.load(f)
        except (IOError, ValueError):
            return {}

    def set_classic_layout(self, layout):
        try:
            with open(self._classic_layout_file(), 'w') as f:
                json.dump(layout, f)
        except IOError:
            logging.warning('Failed to save the classic skin layout')

    def skins_dir(self):
        return os.path.join(GLib.get_user_data_dir(), 'pyrrha', 'skins')

    def bundled_skins_dir(self):
        return os.path.join(os.path.dirname(__file__), 'skins')

    def themes_dir(self):
        """The project's themes/ folder — a curated skin collection scanned into
        the library alongside the bundled and user skins (absent when installed,
        which is fine: available_skins skips missing directories)."""
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'themes')

    def available_skins(self):
        """[(name, path)] of skins: those bundled with Pyrrha, the project's
        themes/ collection, and the user's (~/.local/share/pyrrha/skins) —
        .wsz files and folders with a main.bmp."""
        out, seen = [], set()
        for base in (self.bundled_skins_dir(), self.themes_dir(), self.skins_dir()):
            try:
                entries = sorted(os.listdir(base))
            except OSError:
                continue
            for name in entries:
                full = os.path.join(base, name)
                label = name
                if os.path.isdir(full):
                    try:
                        if not any(f.lower() == 'main.bmp' for f in os.listdir(full)):
                            continue
                    except OSError:
                        continue
                elif name.lower().endswith(('.wsz', '.zip')):
                    label = os.path.splitext(name)[0]
                else:
                    continue
                if label.lower() not in seen:
                    out.append((label, full))
                    seen.add(label.lower())
        return out

    def install_skin(self, path):
        """Copy a .wsz/.zip skin (or a skin folder) into the user's skins dir so
        it persists in the library. Returns the installed path, or the original
        path if it's already in the library or the copy fails."""
        dest_dir = self.skins_dir()
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError:
            return path
        dest = os.path.join(dest_dir, os.path.basename(os.path.normpath(path)))
        try:
            if os.path.abspath(path) == os.path.abspath(dest) or os.path.exists(dest):
                return dest   # already installed (or a same-named skin exists)
            if os.path.isdir(path):
                shutil.copytree(path, dest)
            else:
                shutil.copy2(path, dest)
            logging.info('Installed skin to %s', dest)
            return dest
        except (OSError, shutil.Error) as e:
            logging.warning('Failed to install skin %s: %s', path, e)
            return path

    def _skin_mode_file(self):
        d = os.path.join(GLib.get_user_config_dir(), 'pyrrha')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, 'skin_mode.txt')

    def get_skin_mode(self):
        try:
            with open(self._skin_mode_file()) as f:
                mode = f.read().strip()
            return mode if mode in ('classic', 'modern') else 'modern'
        except IOError:
            return 'modern'

    def set_skin_mode(self, mode):
        try:
            with open(self._skin_mode_file(), 'w') as f:
                f.write(mode)
        except IOError:
            logging.warning('Failed to save the skin mode')

    def on_gst_error(self, bus, message):
        err, debug = message.parse_error()
        logging.error("Gstreamer error: %s, %s, %s" % (err, debug, err.code))
        if self.current_song:
            self.current_song.message = "Error: " + str(err)
            self.update_song_row()
        self.gstreamer_error = str(err)
        self.gstreamer_errorcount_1 += 1
        if not GstPbutils.install_plugins_installation_in_progress():
            self.next_song()

    def on_gst_buffering(self, bus, message):
        self.react_to_buffering_message(False)
        if self.buffering_timer_id:
            GLib.source_remove(self.buffering_timer_id)
            self.buffering_timer_id = 0
        self.buffering_timer_id = GLib.timeout_add(200, self.react_to_buffering_message, True)

    def react_to_buffering_message(self, from_timeout):
        if from_timeout:
            self.buffering_timer_id = 0
        buffering = self.query_buffer()

        if buffering and self._current_state is not PseudoGst.BUFFERING:
            logging.debug("Buffer underrun")
            self._set_player_state(PseudoGst.BUFFERING)
        elif not buffering and self._current_state is PseudoGst.BUFFERING:
            logging.debug("Buffer overrun")
            if self._buffer_recovery_state is PseudoGst.STOPPED:
                self.play(change_gst_state=True)
            elif self._buffer_recovery_state is PseudoGst.PLAYING:
                self._set_player_state(PseudoGst.PLAYING, change_gst_state=True)
            elif self._buffer_recovery_state is PseudoGst.PAUSED:
                self._set_player_state(PseudoGst.PAUSED, change_gst_state=True)
            self.buffering_finished.emit(self.query_position() or 0)
        return buffering

    def on_gst_volume(self, player, volumespec):
        vol = self.player.get_property('volume')
        GLib.idle_add(self.set_volume_cb, vol)

    def set_volume_cb(self, volume):
        scaled_volume = math.pow(volume, 1.0 / 3.0)
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(int(round(scaled_volume * 100)))
        self.volume_slider.blockSignals(False)

    def on_gst_source(self, player, params):
        soup = player.props.source.props
        proxy = self.get_proxy()
        if proxy and hasattr(soup, 'proxy'):
            scheme, user, password, hostport = parse_proxy(proxy)
            soup.proxy = hostport
            soup.proxy_id = user
            soup.proxy_pw = password

    # ------------------------------------------------------------- row text
    def song_text(self, song):
        title = html.escape(song.title)
        artist = html.escape(song.artist)
        album = html.escape(song.album)
        msg = []
        is_radio = isinstance(song, radio.RadioStation)
        if song is self.current_song:
            song.position = self.query_position()
            if song.bitrate is not None:
                msg.append("%skbit/s" % (song.bitrate))
            if is_radio:
                # A live stream has no length: show LIVE + elapsed, not a ratio.
                msg.append("LIVE")
                if song.position is not None:
                    msg.append(self.format_time(song.position))
                if self.playing is False:
                    msg.append("Paused")
            elif song.position is not None and getattr(song, 'duration', None) is not None:
                pos_str = self.format_time(song.position)
                msg.append("%s / %s" % (pos_str, song.duration_message))
                if self.playing is False:
                    msg.append("Paused")
            if self._current_state is PseudoGst.BUFFERING:
                msg.append("Buffering…")
        if song.message:
            msg.append(song.message)
        status = " - ".join(msg) or " "

        if song.is_ad:
            description = ('<span style="font-size:large; font-weight:bold">'
                          'Commercial Advertisement</span><br>'
                          '<span style="font-weight:bold">Pandora</span>')
        else:
            description = (
                '<span style="font-size:large; font-weight:bold">%s</span><br>'
                'by <span style="font-weight:bold">%s</span><br>'
                '<span style="font-size:small">from <i>%s</i></span>'
                % (title, artist, album))
        return '%s<br><span style="font-size:small">%s</span>' % (
            description, html.escape(status))

    @staticmethod
    def song_icon(song):
        if song.tired:
            return 'tired'
        if song.rating == RATE_LOVE:
            return 'love'
        if song.rating == RATE_BAN:
            return 'ban'
        return None

    def update_song_row(self, song=None):
        if song is None:
            song = self.current_song
        if song is not None and song.index is not None:
            self.songs_model.update_row(
                song.index, html=self.song_text(song), icon=self.song_icon(song))
        return True

    def create_ui_loop(self):
        if not self.ui_loop_timer_id:
            self.ui_loop_timer_id = GLib.timeout_add_seconds(1, self.update_song_row)

    def destroy_ui_loop(self):
        if self.ui_loop_timer_id:
            GLib.source_remove(self.ui_loop_timer_id)
            self.ui_loop_timer_id = 0

    @staticmethod
    def format_time(time_int):
        if time_int is None:
            return None
        time_int //= 1000000000
        s = time_int % 60
        time_int //= 60
        m = time_int % 60
        time_int //= 60
        h = time_int
        if h:
            return "%i:%02i:%02i" % (h, m, s)
        return "%i:%02i" % (m, s)

    # ------------------------------------------------------------- selection
    def selected_song(self):
        idx = self.songs_view.currentIndex()
        if idx.isValid():
            return idx.data(SongRole)

    def start_selected_song(self, *ignore):
        song = self.selected_song()
        if song is None or self.current_song_index is None:
            return False
        # Pandora only lets you jump forward in the endless playlist; static
        # playlists (local files / radio favourites) let you play any row.
        playable = self.static_playlist or song.index > self.current_song_index
        if playable:
            self.start_song(song.index, jump=True)
        return playable

    # --------------------------------------------------------------- ratings
    def _rate_callback(self, song):
        def callback(l):
            self.update_song_row(song)
            self.metadata_changed.emit(song)
        return callback

    def love_song(self, *ignore, song=None):
        if self.source != SOURCE_PANDORA:   # ratings are Pandora-only
            return
        song = song or self.current_song
        if song:
            self.worker_run(song.rate, (RATE_LOVE,), self._rate_callback(song), "Loving song...")

    def ban_song(self, *ignore, song=None):
        if self.source != SOURCE_PANDORA:
            return
        song = song or self.current_song
        if not song:
            return
        self.worker_run(song.rate, (RATE_BAN,), self._rate_callback(song), "Banning song...")
        if song is self.current_song:
            self.next_song()

    def unrate_song(self, *ignore, song=None):
        if self.source != SOURCE_PANDORA:
            return
        song = song or self.current_song
        if song:
            self.worker_run(song.rate, (RATE_NONE,), self._rate_callback(song),
                            "Removing song rating...")

    def tired_song(self, *ignore, song=None):
        if self.source != SOURCE_PANDORA:
            return
        song = song or self.current_song
        if not song:
            return
        self.worker_run(song.set_tired, (), self._rate_callback(song), "Putting song on shelf...")
        if song is self.current_song:
            self.next_song()

    def bookmark_song(self, *ignore, song=None):
        if self.source != SOURCE_PANDORA:
            return
        song = song or self.current_song
        if song:
            self.worker_run(song.bookmark, (), None, "Bookmarking...")

    def bookmark_song_artist(self, *ignore, song=None):
        if self.source != SOURCE_PANDORA:
            return
        song = song or self.current_song
        if song:
            self.worker_run(song.bookmark_artist, (), None, "Bookmarking...")

    def info_song(self, *ignore, song=None):
        song = song or self.current_song
        if self.local_mode:
            if song is not None:
                self.edit_local_tags(song)
            return
        # Pandora → song web page; radio → station homepage (if any).
        if song and song.songDetailURL:
            self.open_url(song.songDetailURL)

    def edit_local_tags(self, song):
        """Open the tag editor for a local file (Alt+3 / playlist menu)."""
        if not local.tags_editable():
            QMessageBox.information(
                self, _('Edit Tags'),
                _('Tag editing needs the “mutagen” package (pip install mutagen).'))
            return
        from .dialogs.tags import TagEditorDialog
        TagEditorDialog(song, self._on_tags_saved, self).show()

    def _on_tags_saved(self, song):
        self.update_song_row(song)
        self.metadata_changed.emit(song)

    def remove_missing_local(self):
        """Drop local rows whose file no longer exists. Returns the count."""
        if not self.local_mode:
            return 0
        missing = [i for i in range(len(self.songs_model))
                   if not os.path.exists(getattr(self.songs_model.song_at(i), 'path', ''))]
        if missing:
            self.remove_songs(missing)
        return len(missing)

    def scan_replaygain_local(self):
        """Scan the local playlist's ReplayGain track gains on a worker thread
        and store them on the songs (applied by the ReplayGain plugin)."""
        if not self.local_mode:
            return
        songs = [self.songs_model.song_at(i) for i in range(len(self.songs_model))]
        by_path = {s.path: s for s in songs if s is not None and getattr(s, 'path', None)}
        paths = list(by_path)
        if not paths:
            return
        self.status_push('net', _('Scanning ReplayGain…'))

        def done(result):
            self.status_pop('net')
            result = result or {}
            for path, gain in result.items():
                s = by_path.get(path)
                if s is not None:
                    s.trackGain = gain
            cur = self.current_song
            if cur is not None and hasattr(self, 'rgvolume'):
                try:
                    self.rgvolume.set_property('fallback-gain', getattr(cur, 'trackGain', 0.0))
                except Exception:
                    pass
            QMessageBox.information(
                self, _('ReplayGain'),
                _('Scanned {} of {} tracks.').format(len(result), len(paths)))

        self.worker_run(local.scan_replaygain, (paths,), done, _('Scanning ReplayGain…'))

    def create_artist_station(self, song):
        user_data = ('artist', html.escape(song.artist))
        self.worker_run('add_station_by_track_token', (song.trackToken, 'artist'),
                        self.on_station_added, user_data=user_data)

    def create_song_station(self, song):
        user_data = ('song', '{} by {}'.format(html.escape(song.title), html.escape(song.artist)))
        self.worker_run('add_station_by_track_token', (song.trackToken, 'song'),
                        self.on_station_added, user_data=user_data)

    # -------------------------------------------------------- context menu
    def on_songs_context_menu(self, pos):
        idx = self.songs_view.indexAt(pos)
        if not idx.isValid():
            return
        self.songs_view.setCurrentIndex(idx)
        song = self.selected_song()
        if song is None:
            return
        menu = QMenu(self)
        if self.source == SOURCE_RADIO:
            # Radio is its own mode: tune in, manage favourites, open homepage.
            menu.addAction(_('Play'), lambda: self.start_song(song.index))
            menu.addSeparator()
            if self.is_radio_favorite(song):
                menu.addAction(_('Remove from Favourites'),
                               lambda: self.remove_radio_favorite(song))
            else:
                menu.addAction(_('Add to Favourites'),
                               lambda: self.add_radio_favorite(song))
            if song.songDetailURL:
                menu.addAction(_('Station Homepage'),
                               lambda: self.open_url(song.songDetailURL))
            menu.exec(self.songs_view.viewport().mapToGlobal(pos))
            return
        if self.local_mode:
            # Ratings/bookmarks/stations are Pandora-only; offer just playback.
            menu.addAction(_('Play'), lambda: self.start_song(song.index))
            menu.exec(self.songs_view.viewport().mapToGlobal(pos))
            return
        if song.rating != RATE_LOVE:
            menu.addAction(_('Love'), lambda: self.love_song(song=song))
        else:
            menu.addAction(_('Unlove'), lambda: self.unrate_song(song=song))
        if song.rating != RATE_BAN:
            menu.addAction(_('Ban'), lambda: self.ban_song(song=song))
        else:
            menu.addAction(_('Unban'), lambda: self.unrate_song(song=song))
        menu.addAction(_('Tired (shelve for a month)'), lambda: self.tired_song(song=song))
        menu.addSeparator()
        menu.addAction(_('Create Station from Artist'), lambda: self.create_artist_station(song))
        menu.addAction(_('Create Station from Song'), lambda: self.create_song_station(song))
        menu.addSeparator()
        menu.addAction(_('Bookmark Song'), lambda: self.bookmark_song(song=song))
        menu.addAction(_('Bookmark Artist'), lambda: self.bookmark_song_artist(song=song))
        menu.addAction(_('Song Information'), lambda: self.info_song(song=song))
        menu.exec(self.songs_view.viewport().mapToGlobal(pos))

    # --------------------------------------------------------------- volume
    def set_player_volume(self, value):
        self.player.set_property("volume", math.pow(value, 3))

    def set_player_balance(self, value):
        """Set stereo balance in [-1, 1] (-1 full left, 0 center, +1 right)."""
        if self.panorama is not None:
            self.panorama.set_property("panorama", max(-1.0, min(1.0, float(value))))

    def on_volume_slider_changed(self, value):
        v = value / 100.0
        self.set_player_volume(v)
        self.settings.set_double('volume', v)

    def adjust_volume(self, amount):
        self.volume_slider.setValue(self.volume_slider.value() + amount)

    def volume_up(self, *ignore):
        self.adjust_volume(+2)

    def volume_down(self, *ignore):
        self.adjust_volume(-2)

    # --------------------------------------------------------------- dialogs
    def open_url(self, url):
        logging.info("Opening URL {}".format(url))
        QDesktopServices.openUrl(QUrl(url))

    def error_dialog(self, message, retry_cb, submsg=None):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setText(message or '')
        if submsg:
            box.setInformativeText(submsg)
        box.addButton(_('Preferences'), QMessageBox.ActionRole)
        retry_btn = None
        if retry_cb is not None:
            retry_btn = box.addButton(_('Retry'), QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if retry_btn is not None and clicked is retry_btn:
            self.gstreamer_errorcount_2 = 0
            logging.info("Manual retry")
            retry_cb()
        elif clicked is not None and box.buttonRole(clicked) == QMessageBox.ActionRole:
            self.show_preferences()

    def fatal_error_dialog(self, message, submsg):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setText(message or '')
        if submsg:
            box.setInformativeText(submsg)
        box.exec()
        self.app.quit()

    def api_update_dialog(self):
        reply = QMessageBox.warning(
            self, _('Pyrrha'),
            _('Pyrrha is out of date and can no longer connect to Pandora. '
              'Please update Pyrrha.'),
            QMessageBox.Ok | QMessageBox.Cancel)
        if reply == QMessageBox.Ok:
            self.open_url("https://github.com/k5blazerfl/Pyrrha")
        self.app.quit()

    def on_prefs_finished(self, result):
        if not self.settings['email']:
            self.close()

    def show_about(self):
        AboutDialog(__version__, self).exec()

    def show_preferences(self):
        self.sync_explicit_content_filter_setting()
        self.prefs_dlg.load()
        self.prefs_dlg.show()

    def show_stations(self):
        if self.stations_dlg is not None:
            self.stations_dlg.raise_()
            self.stations_dlg.activateWindow()
        else:
            self.stations_dlg = StationsDialog(self, self)
            self.stations_dlg.finished.connect(self._on_stations_dlg_closed)
            self.stations_dlg.show()

    def _on_stations_dlg_closed(self, *ignore):
        self.stations_dlg = None

    def show_radio(self):
        if getattr(self, 'radio_dlg', None) is not None:
            self.radio_dlg.raise_()
            self.radio_dlg.activateWindow()
        else:
            self.radio_dlg = RadioDialog(self, self)
            self.radio_dlg.finished.connect(self._on_radio_dlg_closed)
            self.radio_dlg.show()

    def _on_radio_dlg_closed(self, *ignore):
        self.radio_dlg = None

    # --------------------------------------------------------------- close
    def closeEvent(self, event):
        # Plugins (e.g. the tray icon) may intercept the close to hide the
        # window instead of quitting. An interceptor returns True to do so.
        for interceptor in self.close_interceptors:
            try:
                if interceptor():
                    event.ignore()
                    self.hide()
                    return
            except Exception:
                logging.exception('close interceptor failed')
        self._save_pandora_queue()
        self.stop()
        super().closeEvent(event)
        self.app.quit()
