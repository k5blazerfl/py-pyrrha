# -*- coding: utf-8 -*-
# Pyrrha - a Qt port of Pithos, a native Pandora Radio client.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3, as published
# by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.

__version__ = "0.4.69-qt"

# Pyrrha's own application id. Everything identity-related derives from it:
# the GSettings schema (+ .plugin / .plugin-enabled children), the libsecret
# account schema (SETTINGS_SCHEMA + ".Account"), the .desktop file and icon
# name. A one-time migration (pyrrha.migrate) imports an existing Pithos config.
APP_ID = 'io.github.k5blazerfl.Pyrrha'
SETTINGS_SCHEMA = APP_ID
