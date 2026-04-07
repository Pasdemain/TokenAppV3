import random
import json
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, redirect, url_for, session, flash, request, jsonify
from psycopg2.extras import RealDictCursor
from database import get_db_connection
from auth import login_required

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
@login_required
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

    # Available categories with card counts
    cur.execute("""
        SELECT fc.id, fc.name, fc.icon, COUNT(f.id) as card_count
        FROM flashcard_categories fc
        LEFT JOIN flashcards f ON f.category_id = fc.id
        GROUP BY fc.id, fc.name, fc.icon
        ORDER BY fc.name
    """)
    categories = cur.fetchall()

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
                           categories=categories,
                           languages=languages,
                           lang_pairs=lang_pairs)


# ── Session (choose category + languages) ────────────────────────────────────

@flashcard_bp.route('/flashcards/session', methods=['GET', 'POST'])
@login_required
def flashcard_session():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT id, code, name, flag_emoji FROM languages ORDER BY name")
    languages = cur.fetchall()

    cur.execute("""
        SELECT fc.id, fc.name, fc.icon, COUNT(f.id) as card_count
        FROM flashcard_categories fc
        LEFT JOIN flashcards f ON f.category_id = fc.id
        GROUP BY fc.id, fc.name, fc.icon
        ORDER BY fc.name
    """)
    categories = cur.fetchall()

    if request.method == 'POST':
        source_lang = request.form.get('source_lang', '').strip()
        target_lang = request.form.get('target_lang', '').strip()
        category_id = request.form.get('category_id', type=int)

        if not source_lang or not target_lang:
            flash('Please select both languages.', 'error')
        elif source_lang == target_lang:
            flash('Source and target languages must be different.', 'error')
        else:
            # Store session preferences
            session['fc_source_lang'] = source_lang
            session['fc_target_lang'] = target_lang
            session['fc_category_id'] = category_id
            cur.close()
            conn.close()
            return redirect(url_for('flashcards.review'))

    cur.close()
    conn.close()
    return render_template('flashcard_session.html',
                           languages=languages,
                           categories=categories)


# ── Review (show card + QCM) ─────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/review')
@login_required
def review():
    source_lang = session.get('fc_source_lang')
    target_lang = session.get('fc_target_lang')

    if not source_lang or not target_lang:
        flash('Please start a session first.', 'warning')
        return redirect(url_for('flashcards.flashcard_session'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()

    # Build query — optionally filter by category
    category_id = session.get('fc_category_id')
    if category_id:
        cur.execute("""
            SELECT uf.id as user_flashcard_id, uf.leitner_box, uf.next_review_date,
                   f.id as flashcard_id, f.translations, f.audio_hint,
                   fc.name as category_name, fc.icon as category_icon
            FROM user_flashcards uf
            JOIN flashcards f ON uf.flashcard_id = f.id
            LEFT JOIN flashcard_categories fc ON f.category_id = fc.id
            WHERE uf.user_id = %s AND uf.source_lang = %s AND uf.target_lang = %s
              AND uf.next_review_date <= %s AND f.category_id = %s
            ORDER BY uf.leitner_box ASC, uf.next_review_date ASC
            LIMIT 1
        """, (session['user_id'], source_lang, target_lang, today, category_id))
    else:
        cur.execute("""
            SELECT uf.id as user_flashcard_id, uf.leitner_box, uf.next_review_date,
                   f.id as flashcard_id, f.translations, f.audio_hint,
                   fc.name as category_name, fc.icon as category_icon
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
        # Count remaining for stats
        if category_id:
            cur.execute("""
                SELECT COUNT(*) as cnt FROM user_flashcards uf
                JOIN flashcards f ON uf.flashcard_id = f.id
                WHERE uf.user_id = %s AND uf.source_lang = %s AND uf.target_lang = %s
                  AND f.category_id = %s
            """, (session['user_id'], source_lang, target_lang, category_id))
        else:
            cur.execute("""
                SELECT COUNT(*) as cnt FROM user_flashcards
                WHERE user_id = %s AND source_lang = %s AND target_lang = %s
            """, (session['user_id'], source_lang, target_lang))
        total_in_pair = cur.fetchone()['cnt']
        cur.close()
        conn.close()
        return render_template('flashcard_review.html',
                               card=None, total_in_pair=total_in_pair,
                               source_lang=source_lang, target_lang=target_lang,
                               languages=[], options=[])

    translations = card['translations'] if isinstance(card['translations'], dict) else json.loads(card['translations'])
    front_word = translations.get(source_lang, '???')
    correct_answer = translations.get(target_lang, '???')

    # Fetch distractors for this card + target language
    cur.execute("""
        SELECT distractor_text FROM flashcard_distractors
        WHERE flashcard_id = %s AND language_code = %s
        ORDER BY RANDOM() LIMIT 2
    """, (card['flashcard_id'], target_lang))
    distractors = [r['distractor_text'] for r in cur.fetchall()]

    # If not enough distractors, grab random translations from other cards
    if len(distractors) < 2:
        cur.execute("""
            SELECT DISTINCT translations->>%s as word
            FROM flashcards
            WHERE id != %s AND translations ? %s
            ORDER BY RANDOM() LIMIT %s
        """, (target_lang, card['flashcard_id'], target_lang, 2 - len(distractors)))
        for r in cur.fetchall():
            if r['word'] and r['word'] != correct_answer:
                distractors.append(r['word'])

    options = [correct_answer] + distractors[:2]
    random.shuffle(options)

    # Count due cards for progress
    if category_id:
        cur.execute("""
            SELECT COUNT(*) as cnt FROM user_flashcards uf
            JOIN flashcards f ON uf.flashcard_id = f.id
            WHERE uf.user_id = %s AND uf.source_lang = %s AND uf.target_lang = %s
              AND uf.next_review_date <= %s AND f.category_id = %s
        """, (session['user_id'], source_lang, target_lang, today, category_id))
    else:
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
                           front_word=front_word,
                           correct_answer=correct_answer,
                           options=options,
                           remaining=remaining,
                           source_lang=source_lang,
                           target_lang=target_lang,
                           lang_info=lang_info)


# ── Answer ───────────────────────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/review/<int:user_flashcard_id>/answer', methods=['POST'])
@login_required
def answer(user_flashcard_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Verify ownership
    cur.execute("""
        SELECT uf.*, f.translations FROM user_flashcards uf
        JOIN flashcards f ON uf.flashcard_id = f.id
        WHERE uf.id = %s AND uf.user_id = %s
    """, (user_flashcard_id, session['user_id']))
    uf = cur.fetchone()

    if not uf:
        cur.close()
        conn.close()
        return jsonify({'error': 'Card not found'}), 404

    chosen = request.form.get('answer', '').strip()
    translations = uf['translations'] if isinstance(uf['translations'], dict) else json.loads(uf['translations'])
    target_lang = session.get('fc_target_lang', '')
    correct_answer = translations.get(target_lang, '')

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


# ── Add cards to collection ──────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/add/<int:category_id>', methods=['POST'])
@login_required
def add_cards(category_id):
    source_lang = request.form.get('source_lang') or session.get('fc_source_lang', '')
    target_lang = request.form.get('target_lang') or session.get('fc_target_lang', '')

    if not source_lang or not target_lang:
        flash('Please select languages first.', 'error')
        return redirect(url_for('flashcards.flashcard_session'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()

    # Select up to 10 cards from this category not already in user's collection for this lang pair
    cur.execute("""
        SELECT f.id FROM flashcards f
        WHERE f.category_id = %s
          AND f.translations ? %s AND f.translations ? %s
          AND f.id NOT IN (
              SELECT flashcard_id FROM user_flashcards
              WHERE user_id = %s AND source_lang = %s AND target_lang = %s
          )
        ORDER BY RANDOM()
        LIMIT 10
    """, (category_id, source_lang, target_lang,
          session['user_id'], source_lang, target_lang))
    cards = cur.fetchall()

    if not cards:
        flash('No new cards available in this category for your language pair.', 'info')
    else:
        for card in cards:
            cur.execute("""
                INSERT INTO user_flashcards (user_id, flashcard_id, source_lang, target_lang, leitner_box, next_review_date)
                VALUES (%s, %s, %s, %s, 1, %s)
            """, (session['user_id'], card['id'], source_lang, target_lang, today))
        conn.commit()
        flash(f'{len(cards)} cards added to your collection!', 'success')

    cur.close()
    conn.close()
    return redirect(url_for('flashcards.flashcards_home'))


# ── Admin: Import JSON ───────────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/admin/import', methods=['POST'])
def admin_import():
    if request.form.get('password') != 'Tom123':
        flash('Incorrect admin password!', 'error')
        return redirect(url_for('admin'))

    json_data = request.form.get('json_data', '').strip()
    if not json_data:
        flash('No JSON data provided.', 'error')
        return redirect(url_for('admin'))

    try:
        cards = json.loads(json_data)
    except json.JSONDecodeError as e:
        flash(f'Invalid JSON: {e}', 'error')
        return redirect(url_for('admin'))

    if not isinstance(cards, list):
        flash('JSON must be an array of card objects.', 'error')
        return redirect(url_for('admin'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    imported = 0

    try:
        for item in cards:
            category_name = item.get('category', '').strip()
            translations = item.get('translations', {})
            distractors = item.get('distractors', {})

            if not category_name or not translations:
                continue

            # Get or create category
            cur.execute("SELECT id FROM flashcard_categories WHERE name = %s", (category_name,))
            cat = cur.fetchone()
            if cat:
                cat_id = cat['id']
            else:
                cur.execute(
                    "INSERT INTO flashcard_categories (name) VALUES (%s) RETURNING id",
                    (category_name,)
                )
                cat_id = cur.fetchone()['id']

            # Insert flashcard
            cur.execute("""
                INSERT INTO flashcards (category_id, translations)
                VALUES (%s, %s) RETURNING id
            """, (cat_id, json.dumps(translations)))
            flashcard_id = cur.fetchone()['id']

            # Insert distractors
            for lang_code, distractor_list in distractors.items():
                for d_text in distractor_list:
                    cur.execute("""
                        INSERT INTO flashcard_distractors (flashcard_id, language_code, distractor_text)
                        VALUES (%s, %s, %s)
                    """, (flashcard_id, lang_code, d_text))

            # Auto-register languages from translations
            for lang_code in translations.keys():
                cur.execute("SELECT id FROM languages WHERE code = %s", (lang_code,))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO languages (code, name) VALUES (%s, %s)",
                        (lang_code, lang_code.upper())
                    )

            imported += 1

        conn.commit()
        flash(f'Successfully imported {imported} flashcards!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Import error: {e}', 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('admin'))


# ── Admin: Manage Leitner intervals ─────────────────────────────────────────

@flashcard_bp.route('/flashcards/admin/intervals', methods=['GET', 'POST'])
def admin_intervals():
    if request.method == 'POST':
        if request.form.get('password') != 'Tom123':
            flash('Incorrect admin password!', 'error')
            return redirect(url_for('admin'))

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
            flash('Leitner intervals updated!', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'Error updating intervals: {e}', 'error')
        finally:
            cur.close()
            conn.close()

    return redirect(url_for('admin'))


# ── Admin: Manage languages ─────────────────────────────────────────────────

@flashcard_bp.route('/flashcards/admin/languages/add', methods=['POST'])
def admin_add_language():
    if request.form.get('password') != 'Tom123':
        flash('Incorrect admin password!', 'error')
        return redirect(url_for('admin'))

    code = request.form.get('code', '').strip().lower()
    name = request.form.get('name', '').strip()
    flag_emoji = request.form.get('flag_emoji', '').strip()

    if not code or not name:
        flash('Language code and name are required.', 'error')
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
        flash(f'Language "{name}" added!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error adding language: {e}', 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('admin'))


@flashcard_bp.route('/flashcards/admin/languages/<int:lang_id>/delete', methods=['POST'])
def admin_delete_language(lang_id):
    if request.form.get('password') != 'Tom123':
        flash('Incorrect admin password!', 'error')
        return redirect(url_for('admin'))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM languages WHERE id = %s", (lang_id,))
    conn.commit()
    cur.close()
    conn.close()

    flash('Language deleted.', 'success')
    return redirect(url_for('admin'))
