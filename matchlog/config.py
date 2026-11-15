"""Defaults for a clean local installation."""

RETENTION_LIMITS = {
    'cleanup_temporary_days': 1,
    'cleanup_review_days': 1,
    'cleanup_original_days': 1,
    'highlight_days': 0,
}
MAX_RETENTION_DAYS = 3650
HIGHLIGHT_LIMITS = {
    'highlight_kills': 500,
    'highlight_team_kills': 500,
    'highlight_damage': 100_000,
    'highlight_rp': 3000,
}

SETTLEMENT_TONES = ('end', 'summary', 'rank')
THEMES = ('dark', 'slate', 'light')

DEFAULTS = {
    'self_name': '',
    'friends': [],
    'season': 'S31',
    'split': '1',
    'auto_switch': False,
    'initial_rp': None,
    'initial_rp_at': None,
    'game_mode': 'ranked',
    'detected_mode': 'unknown',
    'theme': 'dark',
    'auto_cleanup': True,
    'cleanup_temporary_days': 1,
    'cleanup_review_days': 2,
    'cleanup_original_days': 3,
    'highlight_days': 0,
    'highlight_kills': 5,
    'highlight_team_kills': 10,
    'highlight_damage': 1500,
    'highlight_rp': 200,
    'auto_season': True,
    'start_on_login': False,
    'debug_mode': True,
    'low_impact': False,
    'reduce_motion': False,
    'show_hero_board': True,
    'settlement_tone': 'rank',
    'sound_end_enabled': True,
    'sound_summary_enabled': True,
    'sound_rank_enabled': True,
    'notify_detect_enabled': True,
    'notify_success_enabled': True,
    'notify_pending_enabled': True,
    'notify_detect_seconds': 3,
    'notify_success_seconds': 5,
    'notify_pending_seconds': 5,
}
