import json
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, session, flash, request, jsonify
from psycopg2.extras import RealDictCursor, execute_values
from database import get_db_connection
from auth import login_required

competency_bp = Blueprint('competency', __name__)

# ── Adaptive engine constants ───────────────────────────────────────────────

MAX_QUESTIONS = 50
MIN_QUESTIONS_EARLY_STOP = 25
CONVERGENCE_WINDOW = 10
CONVERGENCE_THRESHOLD = 25.0
INITIAL_SCORE = 400.0
INITIAL_STEP = 100.0
STEP_DECAY = 0.82
MIN_STEP = 15.0


def score_to_level(score):
    if score < 200:
        return 'A1'
    if score < 400:
        return 'A2'
    if score < 600:
        return 'B1'
    return 'B2'


def find_closest_question(cur, skill, answered_ids, score, lang):
    """Find the unused question closest to the estimated score for a given skill."""
    if answered_ids:
        cur.execute("""
            SELECT id, question_id, skill, difficulty_score, content
            FROM competency_questions
            WHERE skill = %s AND id != ALL(%s) AND content->'question' ? %s
            ORDER BY ABS(difficulty_score - %s) ASC
            LIMIT 1
        """, (skill, answered_ids, lang, int(score)))
    else:
        cur.execute("""
            SELECT id, question_id, skill, difficulty_score, content
            FROM competency_questions
            WHERE skill = %s AND content->'question' ? %s
            ORDER BY ABS(difficulty_score - %s) ASC
            LIMIT 1
        """, (skill, lang, int(score)))
    return cur.fetchone()


# ── Home ────────────────────────────────────────────────────────────────────

@competency_bp.route('/competency')
@login_required
def competency_home():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Past tests for this user
    cur.execute("""
        SELECT id, target_lang, status, estimated_score, final_level,
               questions_count, correct_count, started_at, completed_at
        FROM competency_tests
        WHERE user_id = %s
        ORDER BY started_at DESC
        LIMIT 10
    """, (session['user_id'],))
    past_tests = cur.fetchall()

    # Check for in-progress test
    cur.execute("""
        SELECT id, target_lang FROM competency_tests
        WHERE user_id = %s AND status = 'in_progress'
        ORDER BY started_at DESC LIMIT 1
    """, (session['user_id'],))
    active_test = cur.fetchone()

    # Available languages from question pool
    cur.execute("""
        SELECT DISTINCT jsonb_object_keys(content->'question') as lang
        FROM competency_questions
        ORDER BY lang
    """)
    available_langs = [r['lang'] for r in cur.fetchall()]

    # Question pool stats
    cur.execute("""
        SELECT skill, level_hint, COUNT(*) as cnt
        FROM competency_questions
        GROUP BY skill, level_hint
        ORDER BY skill, level_hint
    """)
    pool_stats = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('competency_home.html',
                           past_tests=past_tests,
                           active_test=active_test,
                           available_langs=available_langs,
                           pool_stats=pool_stats)


# ── Start test ──────────────────────────────────────────────────────────────

@competency_bp.route('/competency/start', methods=['POST'])
@login_required
def start_test():
    target_lang = request.form.get('target_lang', '').strip()
    if not target_lang:
        flash('Please select a language.', 'error')
        return redirect(url_for('competency.competency_home'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Check question pool has questions for this language
    cur.execute("""
        SELECT COUNT(*) as cnt FROM competency_questions
        WHERE content->'question' ? %s
    """, (target_lang,))
    if cur.fetchone()['cnt'] == 0:
        flash('No questions available for this language.', 'error')
        cur.close()
        conn.close()
        return redirect(url_for('competency.competency_home'))

    # Cancel any existing in-progress test
    cur.execute("""
        UPDATE competency_tests SET status = 'abandoned'
        WHERE user_id = %s AND status = 'in_progress'
    """, (session['user_id'],))

    # Create new test
    cur.execute("""
        INSERT INTO competency_tests (user_id, target_lang, estimated_score, step_size)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """, (session['user_id'], target_lang, INITIAL_SCORE, INITIAL_STEP))
    test_id = cur.fetchone()['id']

    conn.commit()
    cur.close()
    conn.close()

    return redirect(url_for('competency.test_page', test_id=test_id))


# ── Test page (SPA shell) ──────────────────────────────────────────────────

@competency_bp.route('/competency/test/<int:test_id>')
@login_required
def test_page(test_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT * FROM competency_tests
        WHERE id = %s AND user_id = %s
    """, (test_id, session['user_id']))
    test = cur.fetchone()

    cur.close()
    conn.close()

    if not test:
        flash('Test not found.', 'error')
        return redirect(url_for('competency.competency_home'))

    if test['status'] != 'in_progress':
        return redirect(url_for('competency.result_page', test_id=test_id))

    return render_template('competency_test.html', test=test)


# ── Next question (JSON) ───────────────────────────────────────────────────

@competency_bp.route('/competency/test/<int:test_id>/next')
@login_required
def next_question(test_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT * FROM competency_tests
        WHERE id = %s AND user_id = %s
    """, (test_id, session['user_id']))
    test = cur.fetchone()

    if not test or test['status'] != 'in_progress':
        cur.close()
        conn.close()
        return jsonify({'done': True, 'result_url': url_for('competency.result_page', test_id=test_id)})

    # Get already-answered question IDs
    cur.execute("SELECT question_id FROM competency_answers WHERE test_id = %s", (test_id,))
    answered_ids = [r['question_id'] for r in cur.fetchall()]

    # Determine preferred skill (alternate reading/listening)
    cur.execute("""
        SELECT cq.skill FROM competency_answers ca
        JOIN competency_questions cq ON ca.question_id = cq.id
        WHERE ca.test_id = %s ORDER BY ca.answered_at DESC LIMIT 1
    """, (test_id,))
    last = cur.fetchone()
    preferred = 'listening' if (last and last['skill'] == 'reading') else 'reading'

    lang = test['target_lang']
    score = test['estimated_score']

    # Find closest question for preferred skill, fallback to other
    question = find_closest_question(cur, preferred, answered_ids, score, lang)
    if not question:
        other = 'reading' if preferred == 'listening' else 'listening'
        question = find_closest_question(cur, other, answered_ids, score, lang)

    if not question:
        # No more questions — force complete
        final_level = score_to_level(score)
        cur.execute("""
            UPDATE competency_tests SET status = 'completed', final_level = %s, completed_at = NOW()
            WHERE id = %s
        """, (final_level, test_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'done': True, 'result_url': url_for('competency.result_page', test_id=test_id)})

    content = question['content'] if isinstance(question['content'], dict) else json.loads(question['content'])

    resp = {
        'done': False,
        'question_db_id': question['id'],
        'skill': question['skill'],
        'question_text': content['question'][lang],
        'options': content['options'][lang],
        'progress': test['questions_count'],
        'max_questions': MAX_QUESTIONS
    }

    if question['skill'] == 'reading':
        resp['text'] = content.get('text', {}).get(lang, '')
    else:
        audio_data = content.get('audio', {}).get(lang, {})
        if isinstance(audio_data, dict):
            resp['tts_text'] = audio_data.get('text', '')
            resp['tts_voice'] = audio_data.get('tts_voice', '')
        else:
            resp['tts_text'] = str(audio_data)
            resp['tts_voice'] = ''

    cur.close()
    conn.close()
    return jsonify(resp)


# ── Submit answer (JSON) ───────────────────────────────────────────────────

@competency_bp.route('/competency/test/<int:test_id>/answer', methods=['POST'])
@login_required
def submit_answer(test_id):
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    question_db_id = data.get('question_id')
    chosen = data.get('answer', '')

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Verify test
    cur.execute("""
        SELECT * FROM competency_tests
        WHERE id = %s AND user_id = %s AND status = 'in_progress'
    """, (test_id, session['user_id']))
    test = cur.fetchone()
    if not test:
        cur.close()
        conn.close()
        return jsonify({'error': 'Test not found or already completed'}), 404

    # Get question
    cur.execute("SELECT * FROM competency_questions WHERE id = %s", (question_db_id,))
    question = cur.fetchone()
    if not question:
        cur.close()
        conn.close()
        return jsonify({'error': 'Question not found'}), 404

    content = question['content'] if isinstance(question['content'], dict) else json.loads(question['content'])
    lang = test['target_lang']
    correct = content['answer'][lang]
    is_correct = (chosen == correct)

    # Update adaptive score
    score = test['estimated_score']
    step = test['step_size']

    if is_correct:
        score = min(799, score + step)
    else:
        score = max(0, score - step)

    step = max(MIN_STEP, step * STEP_DECAY)

    questions_count = test['questions_count'] + 1
    correct_count = test['correct_count'] + (1 if is_correct else 0)

    # Record answer
    cur.execute("""
        INSERT INTO competency_answers (test_id, question_id, chosen_answer, is_correct, score_after)
        VALUES (%s, %s, %s, %s, %s)
    """, (test_id, question_db_id, chosen, is_correct, score))

    # Update test state
    cur.execute("""
        UPDATE competency_tests
        SET estimated_score = %s, step_size = %s, questions_count = %s, correct_count = %s
        WHERE id = %s
    """, (score, step, questions_count, correct_count, test_id))

    # Check stop conditions
    is_done = False
    if questions_count >= MAX_QUESTIONS:
        is_done = True
    elif questions_count >= MIN_QUESTIONS_EARLY_STOP:
        cur.execute("""
            SELECT score_after FROM competency_answers
            WHERE test_id = %s ORDER BY answered_at DESC LIMIT %s
        """, (test_id, CONVERGENCE_WINDOW))
        recent = [r['score_after'] for r in cur.fetchall()]
        if len(recent) >= CONVERGENCE_WINDOW:
            mean = sum(recent) / len(recent)
            std_dev = (sum((s - mean) ** 2 for s in recent) / len(recent)) ** 0.5
            if std_dev < CONVERGENCE_THRESHOLD:
                is_done = True

    if is_done:
        final_level = score_to_level(score)
        cur.execute("""
            UPDATE competency_tests SET status = 'completed', final_level = %s, completed_at = NOW()
            WHERE id = %s
        """, (final_level, test_id))

    conn.commit()
    cur.close()
    conn.close()

    result = {
        'is_correct': is_correct,
        'correct_answer': correct,
        'questions_done': questions_count,
        'is_done': is_done
    }
    if is_done:
        result['result_url'] = url_for('competency.result_page', test_id=test_id)

    return jsonify(result)


# ── Result page ─────────────────────────────────────────────────────────────

@competency_bp.route('/competency/result/<int:test_id>')
@login_required
def result_page(test_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT * FROM competency_tests
        WHERE id = %s AND user_id = %s
    """, (test_id, session['user_id']))
    test = cur.fetchone()

    if not test:
        flash('Test not found.', 'error')
        cur.close()
        conn.close()
        return redirect(url_for('competency.competency_home'))

    if test['status'] == 'in_progress':
        cur.close()
        conn.close()
        return redirect(url_for('competency.test_page', test_id=test_id))

    # Per-skill stats
    cur.execute("""
        SELECT cq.skill,
               COUNT(*) as total,
               SUM(CASE WHEN ca.is_correct THEN 1 ELSE 0 END) as correct
        FROM competency_answers ca
        JOIN competency_questions cq ON ca.question_id = cq.id
        WHERE ca.test_id = %s
        GROUP BY cq.skill
    """, (test_id,))
    skill_stats = {r['skill']: r for r in cur.fetchall()}

    # Score progression for chart
    cur.execute("""
        SELECT ca.score_after, cq.skill
        FROM competency_answers ca
        JOIN competency_questions cq ON ca.question_id = cq.id
        WHERE ca.test_id = %s
        ORDER BY ca.answered_at ASC
    """, (test_id,))
    progression = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('competency_result.html',
                           test=test,
                           skill_stats=skill_stats,
                           progression=progression)


# ── Admin: Import questions JSON ────────────────────────────────────────────

@competency_bp.route('/competency/admin/import', methods=['POST'])
def admin_import():
    if request.form.get('password') != 'Tom123':
        flash('Incorrect admin password!', 'error')
        return redirect(url_for('admin'))

    json_data = request.form.get('json_data', '').strip()
    if not json_data:
        flash('No JSON data provided.', 'error')
        return redirect(url_for('admin'))

    try:
        questions = json.loads(json_data)
    except json.JSONDecodeError as e:
        flash(f'Invalid JSON: {e}', 'error')
        return redirect(url_for('admin'))

    if not isinstance(questions, list):
        flash('JSON must be an array of question objects.', 'error')
        return redirect(url_for('admin'))

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    imported = 0

    try:
        rows = []
        for item in questions:
            qid = item.get('id', '').strip()
            skill = item.get('skill', '').strip()
            level_hint = item.get('level_hint', '').strip()
            difficulty_score = item.get('difficulty_score')

            if not qid or not skill or difficulty_score is None:
                continue

            rows.append((qid, skill, level_hint, int(difficulty_score), json.dumps(item)))

        if rows:
            execute_values(cur,
                """INSERT INTO competency_questions (question_id, skill, level_hint, difficulty_score, content)
                   VALUES %s ON CONFLICT (question_id) DO UPDATE
                   SET skill = EXCLUDED.skill, level_hint = EXCLUDED.level_hint,
                       difficulty_score = EXCLUDED.difficulty_score, content = EXCLUDED.content""",
                rows)
            imported = len(rows)

        conn.commit()
        flash(f'Successfully imported {imported} competency questions!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Import error: {e}', 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('admin'))
