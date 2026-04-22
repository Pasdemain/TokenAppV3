import json
from pathlib import Path
from flask import session

SUPPORTED_LANGS = ['en', 'fr']
DEFAULT_LANG = 'en'
LANG_LABELS = {
    'en': ('🇬🇧', 'English'),
    'fr': ('🇫🇷', 'Français'),
}

_cache = {}
_translations_dir = Path(__file__).parent / 'translations'


def _load(lang):
    if lang not in _cache:
        path = _translations_dir / f'{lang}.json'
        _cache[lang] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    return _cache[lang]


def current_lang():
    lang = session.get('lang')
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def t(key, **kwargs):
    lang = current_lang()
    value = _load(lang).get(key)
    if value is None and lang != DEFAULT_LANG:
        value = _load(DEFAULT_LANG).get(key)
    if value is None:
        return key
    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError):
            return value
    return value


def init_app(app):
    app.jinja_env.globals['t'] = t
    app.jinja_env.globals['current_lang'] = current_lang
    app.jinja_env.globals['supported_langs'] = SUPPORTED_LANGS
    app.jinja_env.globals['lang_labels'] = LANG_LABELS
