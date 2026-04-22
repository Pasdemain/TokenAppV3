import random
import json
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, redirect, url_for, session, flash, request, jsonify, Response
from psycopg2.extras import RealDictCursor, execute_values
from database import get_db_connection
from auth import feature_required, admin_required
from i18n import t as _t

flashcard_bp = Blueprint('flashcards', __name__)

# Default Leitner intervals (box_number -> days)
DEFAULT_INTERVALS = {1: 1, 2: 2, 3: 4, 4: 7, 5: 14, 6: 30, 7: 90}


def get_leitner_intervals(cur):
    """Fetch Leitner intervals from DB, fallback to defaults."""
    cur.execute("SELECT box_number, days_interval FROM leitner_intervals ORDER BY box_number")
    rows = cur.fetchall()
    if rows:
        return {r['box_number']: r['days_interval'] for r in rows}
    return DEFAULT_INTERVALS.copy()


# ── Dashboard ────────────────────────────────────────────────────────────────

@flashcard_bp.route('/flashcards')
@feature_required('flashcards')
def flashcards_home():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()

    # Cards due today
    cur.execute("""
        SELECT COUNT(*) as cnt FROM user_flashcards
        WHERE user_id = %s AND next_review_date <= %s
    """, (session['user_id'], today))
    due_count = cur.fetchone()['cnt']

    # Stats per Leitner box
    cur.execute("""
        SELECT leitner_box, COUNT(*) as cnt FROM user_flashcards
        WHERE user_id = %s
        GROUP BY leitner_box ORDER BY leitner_box
    """, (session['user_id'],))
    box_stats = cur.fetchall()

    # Total cards in collection
    cur.execute("SELECT COUNT(*) as cnt FROM user_flashcards WHERE user_id = %s",
                (session['user_id'],))
    total_cards = cur.fetchone()['cnt']

    # Available categories split by card_type
    cur.execute("""
        SELECT fc.id, fc.name, fc.icon, f.card_type,
               COUNT(f.id) as card_count,
               SUM(CASE WHEN f.difficulty = 'beginner' THEN 1 ELSE 0 END) as beginner_count,
               SUM(CASE WHEN f.difficulty = 'medium' THEN 1 ELSE 0 END) as medium_count,
               SUM(CASE WHEN f.difficulty = 'confirmed' THEN 1 ELSE 0 END) as confirmed_count
        FROM flashcard_categories fc
        JOIN flashcards f ON f.category_id = fc.id
        GROUP BY fc.id, fc.name, fc.icon, f.card_type
        HAVING COUNT(f.id) > 0
        ORDER BY fc.name
    """)
    all_cats = cur.fetchall()
    reading_categories  = [c for c in all_cats if c['card_type'] == 'reading']
    listening_categories = [c for c in all_cats if c['card_type'] == 'listening']

    # Available languages
    cur.execute("SELECT id, code, name, flag_emoji FROM languages ORDER BY name")
    languages = cur.fetchall()

    # User's active language pairs
    cur.execute("""
        SELECT DISTINCT source_lang, target_lang FROM user_flashcards
        WHERE user_id = %s
    """, (session['user_id'],))
    lang_pairs = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('flashcards.html',
                           due_count=due_count,
                           box_stats=box_stats,
                           total_cards=total_cards,
                           reading_categories=reading_categories,
                           listening_categories=listening_categories,
                           languages=languages,
                           lang_pairs=lang_pairs,
                           current_source=session.get('fc_source_lang'),
                           current_target=session.get('fc_target_lang'))


# ── Switch active language pair ──────────────────────────────────────────────

@flashcard_bp.route('/flashcards/switch-pair', methods=['POST'])
@feature_required('flashcards')
def switch_pair():
    source_lang = request.form.get('source_lang', '').strip()
    target_lang = request.form.get('target_lang', '').strip()

    if not source_lang or not target_lang:
        flash(_t('flashcards.flash_select_both'), 'error')
    elif source_lang == target_lang:
        flash(_t('flashcards.flash_langs_different'), 'error')
    else:
        session['fc_source_lang'] = source_lang
        session['fc_target_lang'] = target_lang
        flash(_t('flashcards.flash_pair_switched', source=source_lang.upper(), target=target_lang.upper()), 'success')

    return redirect(url_for('flashcards.flashcards_home'))


# ── Session (choose languages — only shown if not yet set) ───────────────────

@flashcard_bp.route('/flashcards/session', methods=['GET', 'POST'])
@feature_required('flashcards')
def flashcard_session():
    # If languages already set, skip straight to review
    if request.method == 'GET' and session.get('fc_source_lang') and session.get('fc_target_lang'):
        return redirect(url_for('flashcards.review'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT id, code, name, flag_emoji FROM languages ORDER BY name")
    languages = cur.fetchall()

    if request.method == 'POST':
        source_lang = request.form.get('source_lang', '').strip()
        target_lang = request.form.get('target_lang', '').strip()

        if not source_lang or not target_lang:
            flash(_t('flashcards.flash_select_both'), 'error')
        elif source_lang == target_lang:
            flash(_t('flashcards.flash_langs_different'), 'error')
        else:
            session['fc_source_lang'] = source_lang
            session['fc_target_lang'] = target_lang
            cur.close()
            conn.close()
            return redirect(url_for('flashcards.review'))

    cur.close()
    conn.close()
    return render_template('flashcard_session.html', languages=languages)


# ── Review (show card + QCM) ─────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/review')
@feature_required('flashcards')
def review():
    source_lang = session.get('fc_source_lang')
    target_lang = session.get('fc_target_lang')

    if not source_lang or not target_lang:
        flash(_t('flashcards.flash_start_session_first'), 'warning')
        return redirect(url_for('flashcards.flashcard_session'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()

    # Review ALL cards due for this user + language pair (no category/difficulty filter)
    cur.execute("""
        SELECT uf.id as user_flashcard_id, uf.leitner_box, uf.next_review_date,
               f.id as flashcard_id, f.translations, f.audio_hint, f.difficulty,
               f.card_type, fc.name as category_name, fc.icon as category_icon
        FROM user_flashcards uf
        JOIN flashcards f ON uf.flashcard_id = f.id
        LEFT JOIN flashcard_categories fc ON f.category_id = fc.id
        WHERE uf.user_id = %s AND uf.source_lang = %s AND uf.target_lang = %s
          AND uf.next_review_date <= %s
        ORDER BY uf.leitner_box ASC, uf.next_review_date ASC
        LIMIT 1
    """, (session['user_id'], source_lang, target_lang, today))

    card = cur.fetchone()

    if not card:
        cur.execute("""
            SELECT COUNT(*) as cnt FROM user_flashcards
            WHERE user_id = %s AND source_lang = %s AND target_lang = %s
        """, (session['user_id'], source_lang, target_lang))
        total_in_pair = cur.fetchone()['cnt']

        # Available cards split by card_type
        cur.execute("""
            SELECT fc.id, fc.name, fc.icon, f.card_type,
                   COUNT(f.id) as available_count,
                   SUM(CASE WHEN f.difficulty = 'beginner' THEN 1 ELSE 0 END) as beginner_count,
                   SUM(CASE WHEN f.difficulty = 'medium' THEN 1 ELSE 0 END) as medium_count,
                   SUM(CASE WHEN f.difficulty = 'confirmed' THEN 1 ELSE 0 END) as confirmed_count
            FROM flashcard_categories fc
            JOIN flashcards f ON f.category_id = fc.id
            WHERE f.translations ? %s AND f.translations ? %s
              AND f.id NOT IN (
                  SELECT flashcard_id FROM user_flashcards
                  WHERE user_id = %s AND source_lang = %s AND target_lang = %s
              )
            GROUP BY fc.id, fc.name, fc.icon, f.card_type
            HAVING COUNT(f.id) > 0
            ORDER BY fc.name
        """, (source_lang, target_lang, session['user_id'], source_lang, target_lang))
        all_avail = cur.fetchall()
        avail_reading   = [c for c in all_avail if c['card_type'] == 'reading']
        avail_listening = [c for c in all_avail if c['card_type'] == 'listening']

        cur.close()
        conn.close()
        return render_template('flashcard_review.html',
                               card=None, total_in_pair=total_in_pair,
                               source_lang=source_lang, target_lang=target_lang,
                               avail_reading=avail_reading,
                               avail_listening=avail_listening,
                               options=[])

    translations = card['translations'] if isinstance(card['translations'], dict) else json.loads(card['translations'])
    card_type = card['card_type'] or 'reading'

    # Reading: show source → pick target | Listening: play target → pick source
    if card_type == 'listening':
        front_word    = translations.get(target_lang, '???')   # spoken
        correct_answer = translations.get(source_lang, '???')  # picked
        options_lang  = source_lang
        front_lang    = target_lang
    else:
        front_word    = translations.get(source_lang, '???')
        correct_answer = translations.get(target_lang, '???')
        options_lang  = target_lang
        front_lang    = source_lang

    # Distractors in the options language, same card_type pool
    cur.execute("""
        SELECT distractor_text FROM flashcard_distractors
        WHERE flashcard_id = %s AND language_code = %s
        ORDER BY RANDOM() LIMIT 2
    """, (card['flashcard_id'], options_lang))
    distractors = [r['distractor_text'] for r in cur.fetchall()]

    if len(distractors) < 2:
        cur.execute("""
            SELECT DISTINCT translations->>%s as word
            FROM flashcards
            WHERE id != %s AND translations ? %s AND card_type = %s
            ORDER BY RANDOM() LIMIT %s
        """, (options_lang, card['flashcard_id'], options_lang, card_type, 2 - len(distractors)))
        for r in cur.fetchall():
            if r['word'] and r['word'] != correct_answer:
                distractors.append(r['word'])

    options = [correct_answer] + distractors[:2]
    random.shuffle(options)

    # Count due cards for progress
    cur.execute("""
        SELECT COUNT(*) as cnt FROM user_flashcards
        WHERE user_id = %s AND source_lang = %s AND target_lang = %s
          AND next_review_date <= %s
    """, (session['user_id'], source_lang, target_lang, today))
    remaining = cur.fetchone()['cnt']

    # Get language info for TTS
    cur.execute("SELECT code, name, flag_emoji FROM languages WHERE code IN (%s, %s)",
                (source_lang, target_lang))
    lang_info = {r['code']: r for r in cur.fetchall()}

    cur.close()
    conn.close()

    return render_template('flashcard_review.html',
                           card=card,
                           card_type=card_type,
                           front_word=front_word,
                           front_lang=front_lang,
                           correct_answer=correct_answer,
                           options=options,
                           remaining=remaining,
                           source_lang=source_lang,
                           target_lang=target_lang,
                           lang_info=lang_info)


# ── Next card (JSON for SPA-style transitions) ──────────────────────────────

@flashcard_bp.route('/flashcards/review/next')
@feature_required('flashcards')
def review_next():
    source_lang = session.get('fc_source_lang')
    target_lang = session.get('fc_target_lang')

    if not source_lang or not target_lang:
        return jsonify({'done': True, 'no_session': True})

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()

    cur.execute("""
        SELECT uf.id as user_flashcard_id, uf.leitner_box,
               f.id as flashcard_id, f.translations, f.audio_hint, f.difficulty,
               f.card_type, fc.name as category_name, fc.icon as category_icon
        FROM user_flashcards uf
        JOIN flashcards f ON uf.flashcard_id = f.id
        LEFT JOIN flashcard_categories fc ON f.category_id = fc.id
        WHERE uf.user_id = %s AND uf.source_lang = %s AND uf.target_lang = %s
          AND uf.next_review_date <= %s
        ORDER BY uf.leitner_box ASC, uf.next_review_date ASC
        LIMIT 1
    """, (session['user_id'], source_lang, target_lang, today))
    card = cur.fetchone()

    if not card:
        cur.close()
        conn.close()
        return jsonify({'done': True})

    translations = card['translations'] if isinstance(card['translations'], dict) else json.loads(card['translations'])
    card_type = card['card_type'] or 'reading'

    if card_type == 'listening':
        front_word     = translations.get(target_lang, '???')
        correct_answer = translations.get(source_lang, '???')
        options_lang   = source_lang
        front_lang     = target_lang
    else:
        front_word     = translations.get(source_lang, '???')
        correct_answer = translations.get(target_lang, '???')
        options_lang   = target_lang
        front_lang     = source_lang

    cur.execute("""
        SELECT distractor_text FROM flashcard_distractors
        WHERE flashcard_id = %s AND language_code = %s
        ORDER BY RANDOM() LIMIT 2
    """, (card['flashcard_id'], options_lang))
    distractors = [r['distractor_text'] for r in cur.fetchall()]

    if len(distractors) < 2:
        cur.execute("""
            SELECT DISTINCT translations->>%s as word
            FROM flashcards
            WHERE id != %s AND translations ? %s AND card_type = %s
            ORDER BY RANDOM() LIMIT %s
        """, (options_lang, card['flashcard_id'], options_lang, card_type, 2 - len(distractors)))
        for r in cur.fetchall():
            if r['word'] and r['word'] != correct_answer:
                distractors.append(r['word'])

    options = [correct_answer] + distractors[:2]
    random.shuffle(options)

    # Remaining count
    cur.execute("""
        SELECT COUNT(*) as cnt FROM user_flashcards
        WHERE user_id = %s AND source_lang = %s AND target_lang = %s
          AND next_review_date <= %s
    """, (session['user_id'], source_lang, target_lang, today))
    remaining = cur.fetchone()['cnt']

    cur.close()
    conn.close()

    return jsonify({
        'done': False,
        'user_flashcard_id': card['user_flashcard_id'],
        'card_type': card_type,
        'front_word': front_word,
        'front_lang': front_lang,
        'correct_answer': correct_answer,
        'options': options,
        'leitner_box': card['leitner_box'],
        'difficulty': card['difficulty'] or 'medium',
        'category_name': card['category_name'],
        'category_icon': card['category_icon'],
        'audio_hint': card['audio_hint'],
        'remaining': remaining,
        'flashcard_id': card['flashcard_id']
    })


# ── Answer ───────────────────────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/review/<int:user_flashcard_id>/answer', methods=['POST'])
@feature_required('flashcards')
def answer(user_flashcard_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT uf.*, f.translations, f.card_type FROM user_flashcards uf
        JOIN flashcards f ON uf.flashcard_id = f.id
        WHERE uf.id = %s AND uf.user_id = %s
    """, (user_flashcard_id, session['user_id']))
    uf = cur.fetchone()

    if not uf:
        cur.close()
        conn.close()
        return jsonify({'error': _t('flashcards.error_card_not_found')}), 404

    chosen = request.form.get('answer', '').strip()
    translations = uf['translations'] if isinstance(uf['translations'], dict) else json.loads(uf['translations'])
    source_lang = session.get('fc_source_lang', '')
    target_lang = session.get('fc_target_lang', '')
    card_type   = uf['card_type'] or 'reading'
    # Listening: options are in source_lang; Reading: options are in target_lang
    answer_lang   = source_lang if card_type == 'listening' else target_lang
    correct_answer = translations.get(answer_lang, '')

    is_correct = chosen == correct_answer
    intervals = get_leitner_intervals(cur)
    today = date.today()

    if is_correct:
        new_box = min(uf['leitner_box'] + 1, 7)
    else:
        new_box = 1

    next_review = today + timedelta(days=intervals.get(new_box, 1))

    cur.execute("""
        UPDATE user_flashcards SET leitner_box = %s, next_review_date = %s
        WHERE id = %s
    """, (new_box, next_review, user_flashcard_id))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        'correct': is_correct,
        'correct_answer': correct_answer,
        'new_box': new_box,
        'next_review': next_review.isoformat()
    })


# ── Report a card ────────────────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/report', methods=['POST'])
@feature_required('flashcards')
def report_card():
    data = request.get_json()
    if not data:
        return jsonify({'error': _t('flashcards.error_no_data')}), 400

    flashcard_id = data.get('flashcard_id')
    comment = (data.get('comment') or '').strip()

    if not flashcard_id or not comment:
        return jsonify({'error': _t('flashcards.error_card_id_and_comment')}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO flashcard_reports (user_id, flashcard_id, comment, source_lang, target_lang)
            VALUES (%s, %s, %s, %s, %s)
        """, (session['user_id'], flashcard_id, comment,
              session.get('fc_source_lang'), session.get('fc_target_lang')))
        conn.commit()
    finally:
        cur.close()
        conn.close()

    return jsonify({'ok': True})


# ── Admin: Delete a report ──────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/admin/reports/<int:report_id>/delete', methods=['POST'])
@admin_required
def admin_delete_report(report_id):

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM flashcard_reports WHERE id = %s", (report_id,))
    conn.commit()
    cur.close()
    conn.close()

    flash(_t('flashcards.admin_report_deleted'), 'success')
    return redirect(url_for('admin'))


# ── Add cards to collection ──────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/add/<int:category_id>', methods=['POST'])
@feature_required('flashcards')
def add_cards(category_id):
    source_lang = request.form.get('source_lang') or session.get('fc_source_lang', '')
    target_lang = request.form.get('target_lang') or session.get('fc_target_lang', '')
    difficulty  = request.form.get('difficulty', '').strip()
    card_type   = request.form.get('card_type', 'reading')
    if card_type not in ('reading', 'listening'):
        card_type = 'reading'

    if not source_lang or not target_lang:
        flash(_t('flashcards.flash_select_langs_first'), 'error')
        return redirect(url_for('flashcards.flashcard_session'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()

    add_where = """f.category_id = %s AND f.card_type = %s
          AND f.translations ? %s AND f.translations ? %s
          AND f.id NOT IN (
              SELECT flashcard_id FROM user_flashcards
              WHERE user_id = %s AND source_lang = %s AND target_lang = %s
          )"""
    add_params = [category_id, card_type, source_lang, target_lang,
                  session['user_id'], source_lang, target_lang]

    if difficulty in ('beginner', 'medium', 'confirmed'):
        add_where += " AND f.difficulty = %s"
        add_params.append(difficulty)

    cur.execute(f"""
        SELECT f.id, f.difficulty, f.translations FROM flashcards f
        WHERE {add_where}
        ORDER BY RANDOM()
        LIMIT 10
    """, add_params)
    cards = cur.fetchall()

    if not cards:
        flash(_t('flashcards.flash_no_new_cards'), 'info')
    else:
        for card in cards:
            cur.execute("""
                INSERT INTO user_flashcards (user_id, flashcard_id, source_lang, target_lang, leitner_box, next_review_date, difficulty, translations)
                VALUES (%s, %s, %s, %s, 1, %s, %s, %s)
            """, (session['user_id'], card['id'], source_lang, target_lang, today,
                  card['difficulty'], json.dumps(card['translations']) if isinstance(card['translations'], dict) else card['translations']))
        conn.commit()
        if len(cards) == 1:
            flash(_t('flashcards.flash_cards_added_one', n=len(cards)), 'success')
        else:
            flash(_t('flashcards.flash_cards_added_many', n=len(cards)), 'success')

    cur.close()
    conn.close()

    # If we came from a session, go back to review; otherwise go to dashboard
    if session.get('fc_source_lang') and session.get('fc_target_lang'):
        return redirect(url_for('flashcards.review'))
    return redirect(url_for('flashcards.flashcards_home'))


# ── Admin: Export JSON ───────────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/admin/export')
@admin_required
def admin_export():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT f.id, f.translations, f.difficulty, f.card_type,
                   fc.name as category
            FROM flashcards f
            JOIN flashcard_categories fc ON f.category_id = fc.id
            ORDER BY fc.name, f.id
        """)
        cards = cur.fetchall()

        cur.execute("""
            SELECT flashcard_id, language_code, distractor_text
            FROM flashcard_distractors
            ORDER BY flashcard_id, language_code
        """)
        dist_rows = cur.fetchall()

        dist_map = {}
        for row in dist_rows:
            fid, lc = row['flashcard_id'], row['language_code']
            dist_map.setdefault(fid, {}).setdefault(lc, []).append(row['distractor_text'])

        export = []
        for card in cards:
            t = card['translations'] if isinstance(card['translations'], dict) else json.loads(card['translations'])
            export.append({
                'category':     card['category'],
                'translations': t,
                'difficulty':   card['difficulty'],
                'card_type':    card['card_type'],
                'distractors':  dist_map.get(card['id'], {}),
            })

        return Response(
            json.dumps(export, ensure_ascii=False, indent=2),
            mimetype='application/json',
            headers={'Content-Disposition': 'attachment; filename=flashcards_export.json'}
        )
    except Exception as e:
        flash(_t('flashcards.admin_export_error', err=str(e)), 'error')
        return redirect(url_for('admin'))
    finally:
        cur.close()
        conn.close()


# ── Admin: Import JSON ───────────────────────────────────────────────────────

def _translations_fingerprint(translations):
    """Dedup key for a translations dict: normalized French value when present,
    otherwise a canonical JSON of the full dict as a fallback."""
    fr = translations.get('fr')
    if isinstance(fr, str) and fr.strip():
        return 'fr:' + fr.strip().lower()
    return json.dumps(dict(sorted(translations.items())), ensure_ascii=False)


@flashcard_bp.route('/flashcards/admin/import', methods=['POST'])
@admin_required
def admin_import():

    json_data = request.form.get('json_data', '').strip()
    if not json_data:
        flash(_t('flashcards.admin_no_json'), 'error')
        return redirect(url_for('admin'))

    try:
        cards = json.loads(json_data)
    except json.JSONDecodeError as e:
        flash(_t('flashcards.admin_invalid_json', err=str(e)), 'error')
        return redirect(url_for('admin'))

    if not isinstance(cards, list):
        flash(_t('flashcards.admin_json_must_be_array'), 'error')
        return redirect(url_for('admin'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    inserted_count = 0
    updated_count = 0
    to_update = []
    to_insert = []
    seen_fps = set()

    try:
        # Build fingerprint → id map from all existing cards
        cur.execute("SELECT id, translations FROM flashcards")
        existing = {}
        for row in cur.fetchall():
            t = row['translations'] if isinstance(row['translations'], dict) else json.loads(row['translations'])
            existing[_translations_fingerprint(t)] = row['id']

        # Collect categories and languages needed
        all_categories = set()
        all_lang_codes = set()
        for item in cards:
            cat = item.get('category', '').strip()
            if cat:
                all_categories.add(cat)
            for lc in item.get('translations', {}).keys():
                all_lang_codes.add(lc)
            for lc in item.get('distractors', {}).keys():
                all_lang_codes.add(lc)

        if all_lang_codes:
            execute_values(cur,
                "INSERT INTO languages (code, name) VALUES %s ON CONFLICT (code) DO NOTHING",
                [(lc, lc.upper()) for lc in all_lang_codes])

        if all_categories:
            execute_values(cur,
                "INSERT INTO flashcard_categories (name) VALUES %s ON CONFLICT (name) DO NOTHING",
                [(c,) for c in all_categories])

        cat_map = {}
        if all_categories:
            cur.execute("SELECT id, name FROM flashcard_categories WHERE name = ANY(%s)",
                        (list(all_categories),))
            for row in cur.fetchall():
                cat_map[row['name']] = row['id']

        for item in cards:
            category_name = item.get('category', '').strip()
            translations = item.get('translations', {})
            difficulty = item.get('difficulty', 'medium').strip().lower()
            if difficulty not in ('beginner', 'medium', 'confirmed'):
                difficulty = 'medium'
            card_type = item.get('card_type', 'reading').strip().lower()
            if card_type not in ('reading', 'listening'):
                card_type = 'reading'
            distractors = item.get('distractors', {})

            if not category_name or not translations or category_name not in cat_map:
                continue

            cat_id = cat_map[category_name]
            fp = _translations_fingerprint(translations)

            if fp in seen_fps:
                continue  # duplicate within this import batch
            seen_fps.add(fp)

            trans_json = json.dumps(translations)
            if fp in existing:
                to_update.append((existing[fp], cat_id, trans_json, difficulty, card_type, distractors))
            else:
                to_insert.append((cat_id, trans_json, difficulty, card_type, distractors))

        # Bulk UPDATE existing cards + clear their old distractors in one shot each
        if to_update:
            cur.execute(
                "DELETE FROM flashcard_distractors WHERE flashcard_id = ANY(%s)",
                ([u[0] for u in to_update],)
            )
            execute_values(cur, """
                UPDATE flashcards SET
                    category_id = data.category_id,
                    translations = data.translations::jsonb,
                    difficulty = data.difficulty,
                    card_type = data.card_type
                FROM (VALUES %s) AS data(id, category_id, translations, difficulty, card_type)
                WHERE flashcards.id = data.id
            """, [(fid, cat_id, t, d, ct) for (fid, cat_id, t, d, ct, _) in to_update])

        # Bulk INSERT new cards and collect the generated ids
        new_ids = []
        if to_insert:
            new_ids = execute_values(cur, """
                INSERT INTO flashcards (category_id, translations, difficulty, card_type)
                VALUES %s RETURNING id
            """,
                [(cat_id, t, d, ct) for (cat_id, t, d, ct, _) in to_insert],
                fetch=True
            )

        # Bulk INSERT every distractor (for both updated and new cards)
        all_distractors = []
        for (fid, _, _, _, _, dist) in to_update:
            for lc, dlist in dist.items():
                for d_text in dlist:
                    all_distractors.append((fid, lc, d_text))
        for row, (_, _, _, _, dist) in zip(new_ids, to_insert):
            fid = row['id']
            for lc, dlist in dist.items():
                for d_text in dlist:
                    all_distractors.append((fid, lc, d_text))
        if all_distractors:
            execute_values(cur,
                "INSERT INTO flashcard_distractors (flashcard_id, language_code, distractor_text) VALUES %s",
                all_distractors
            )

        inserted_count = len(to_insert)
        updated_count = len(to_update)

        conn.commit()
        parts = []
        if inserted_count:
            key = 'flashcards.admin_import_added_one' if inserted_count == 1 else 'flashcards.admin_import_added_many'
            parts.append(_t(key, n=inserted_count))
        if updated_count:
            key = 'flashcards.admin_import_updated_one' if updated_count == 1 else 'flashcards.admin_import_updated_many'
            parts.append(_t(key, n=updated_count))
        parts_str = ", ".join(parts) if parts else _t('flashcards.admin_import_no_changes')
        flash(_t('flashcards.admin_import_done', parts=parts_str), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('flashcards.admin_import_error', err=str(e)), 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('admin'))


# ── Admin: Manage Leitner intervals ─────────────────────────────────────────

@flashcard_bp.route('/flashcards/admin/intervals', methods=['GET', 'POST'])
@admin_required
def admin_intervals():
    if request.method == 'POST':

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            for box in range(1, 8):
                days = request.form.get(f'box_{box}', type=int)
                if days and days > 0:
                    cur.execute("""
                        INSERT INTO leitner_intervals (box_number, days_interval)
                        VALUES (%s, %s)
                        ON CONFLICT (box_number) DO UPDATE SET days_interval = EXCLUDED.days_interval
                    """, (box, days))
            conn.commit()
            flash(_t('flashcards.admin_intervals_updated'), 'success')
        except Exception as e:
            conn.rollback()
            flash(_t('flashcards.admin_intervals_error', err=str(e)), 'error')
        finally:
            cur.close()
            conn.close()

    return redirect(url_for('admin'))


# ── Admin: Manage languages ─────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/admin/languages/add', methods=['POST'])
@admin_required
def admin_add_language():

    code = request.form.get('code', '').strip().lower()
    name = request.form.get('name', '').strip()
    flag_emoji = request.form.get('flag_emoji', '').strip()

    if not code or not name:
        flash(_t('flashcards.admin_lang_required'), 'error')
        return redirect(url_for('admin'))

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO languages (code, name, flag_emoji)
            VALUES (%s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, flag_emoji = EXCLUDED.flag_emoji
        """, (code, name, flag_emoji or None))
        conn.commit()
        flash(_t('flashcards.admin_lang_added', name=name), 'success')
    except Exception as e:
        conn.rollback()
        flash(_t('flashcards.admin_lang_add_error', err=str(e)), 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('admin'))


@flashcard_bp.route('/flashcards/admin/languages/<int:lang_id>/delete', methods=['POST'])
@admin_required
def admin_delete_language(lang_id):

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM languages WHERE id = %s", (lang_id,))
    conn.commit()
    cur.close()
    conn.close()

    flash(_t('flashcards.admin_lang_deleted'), 'success')
    return redirect(url_for('admin'))
