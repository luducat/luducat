# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""Dialog windows for luducat"""

from .about import AboutDialog
from .csv_export import CsvExportDialog
from .settings import SettingsDialog
from .plugin_config import PluginConfigDialog
from .tag_editor import TagEditorDialog
from .tag_manager_dialog import TagManagerDialog
from .image_viewer import ImageViewerDialog
from .setup_wizard import SetupWizard
from .game_settings import GameSettingsDialog
from .priority_editor import PriorityEditorDialog
from .metadata_preview import MetadataPreviewDialog
from .update_dialog import UpdateDialog

__all__ = [
    "AboutDialog",
    "CsvExportDialog",
    "SettingsDialog",
    "PluginConfigDialog",
    "TagEditorDialog",
    "TagManagerDialog",
    "ImageViewerDialog",
    "SetupWizard",
    "GameSettingsDialog",
    "PriorityEditorDialog",
    "MetadataPreviewDialog",
    "UpdateDialog",
]
