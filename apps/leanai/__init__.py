import os
import re
import json
import secrets
import time
import uuid
import urllib.request
import urllib.error
from flask import Blueprint, render_template, request, redirect, session, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from .models import (
    create_user, get_user,
    update_user_profile, update_user_avatar, update_user_id, update_user_password,
    skip_user_setup,
    create_preset, get_user_presets, delete_preset,
    create_character, get_user_characters, get_character, update_character_avatar,
    update_character, delete_character, duplicate_character,
    create_message, get_character_messages, clear_character_messages,
    update_message, delete_message, delete_messages_from, get_message,
    get_ai_settings, save_ai_settings,
    get_character_state, save_character_state,
    add_memory, get_memories, clear_memories,
    add_npc, get_character_npcs, get_active_npcs,
    set_npc_active, delete_npc,
    add_generated_image, get_image_history, delete_generated_image,
)
from . import extraction as _ex

bp = Blueprint(
    'leanai',
    __name__,
    url_prefix='/LeanAI',
    template_folder='templates',
    static_folder='static',
)

AVATAR_DIR = os.path.join(os.path.dirname(__file__), 'static', 'avatars')
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
TEXT_EXT = {'.txt', '.md', '.csv'}

NPC_FORMAT = (
    "\n\nWhen someone else is present in the scene, you may speak as them when appropriate. "
    "Prefix their dialogue with [Name:] — for example:\n"
    "[Mom:] No, thank you.\n"
    "For your own dialogue, speak without any prefix."
)


@bp.record_once
def setup(state):
    secret_path = os.path.join(os.path.dirname(__file__), 'secret.txt')
    if os.path.exists(secret_path):
        with open(secret_path) as f:
            key = f.read().strip()
    else:
        key = secrets.token_hex(32)
        with open(secret_path, 'w') as f:
            f.write(key)
    state.app.config['SECRET_KEY'] = key


@bp.before_request
def _require_login():
    if request.endpoint in ('leanai.index', 'leanai.static', 'leanai.login', 'leanai.signup'):
        return
    if not session.get('user_id'):
        return redirect('/LeanAI')


def _logged_in_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return get_user(uid)


def _login_required():
    return _logged_in_user()


# ─── Home ───

@bp.route('/', strict_slashes=False)
def index():
    user = _logged_in_user()
    if not user:
        return render_template('auth.html')

    needs_setup = (not user['name'] and not user['setup_skipped'])
    characters = get_user_characters(user['id'])
    return render_template(
        'dashboard.html',
        user=user,
        characters=characters,
        show_setup=needs_setup,
    )


@bp.route('/login', methods=['POST'])
def login():
    uid = request.form.get('id', '').strip()
    password = request.form.get('password', '')

    if not uid or not password:
        return render_template('auth.html', msg='ID and password are required.')

    user = get_user(uid)
    if not user or not check_password_hash(user['password_hash'], password):
        return render_template('auth.html', msg='Invalid ID or password.')

    session['user_id'] = uid
    return redirect('/LeanAI')


@bp.route('/signup', methods=['POST'])
def signup():
    uid = request.form.get('id', '').strip()
    password = request.form.get('password', '')

    if not uid or not password:
        return render_template('auth.html', msg='ID and password are required.')

    pw_hash = generate_password_hash(password)
    ok = create_user(uid, pw_hash)

    if not ok:
        return render_template('auth.html', msg='ID already taken.')

    session['user_id'] = uid
    return redirect('/LeanAI')


@bp.route('/setup', methods=['POST'])
def setup_profile():
    user = _login_required()
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    update_user_profile(user['id'], name, description)
    return redirect('/LeanAI')


@bp.route('/setup/skip')
def skip_profile():
    user = _logged_in_user()
    if user:
        skip_user_setup(user['id'])
    return redirect('/LeanAI')


@bp.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect('/LeanAI')


# ─── Profile ───

@bp.route('/profile', methods=['GET', 'POST'])
def profile():
    user = _login_required()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        update_user_profile(user['id'], name, description)
        msg = 'Profile saved.'
        return render_template(
            'profile.html', user=get_user(user['id']),
            presets=get_user_presets(user['id']), msg=msg,
        )

    return render_template(
        'profile.html', user=user,
        presets=get_user_presets(user['id']),
    )


@bp.route('/avatar', methods=['POST'])
def avatar():
    user = _login_required()

    file = request.files.get('avatar')
    if not file or not file.filename:
        return redirect('/LeanAI/profile')

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return redirect('/LeanAI/profile')

    filename = secure_filename(f'{user["id"]}{ext}')
    filepath = os.path.join(AVATAR_DIR, filename)

    for old in os.listdir(AVATAR_DIR):
        if old.startswith(secure_filename(user['id']) + '.'):
            os.remove(os.path.join(AVATAR_DIR, old))

    file.save(filepath)
    update_user_avatar(user['id'], filename)
    return redirect('/LeanAI/profile')


# ─── Account ───

@bp.route('/account', methods=['GET', 'POST'])
def account():
    user = _login_required()
    msg = None

    if request.method == 'POST':
        new_id = request.form.get('new_id', '').strip()
        cur_pw = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')

        if not check_password_hash(user['password_hash'], cur_pw):
            return render_template('account.html', user=user, msg='Current password is incorrect.')

        if new_id and new_id != user['id']:
            if not new_id.strip():
                return render_template('account.html', user=user, msg='ID cannot be empty.')
            pw_hash = generate_password_hash(cur_pw)
            ok = update_user_id(user['id'], new_id, pw_hash)
            if not ok:
                return render_template('account.html', user=user, msg='That ID is already taken.')
            session['user_id'] = new_id
            user = get_user(new_id)
            msg = 'ID changed.'

        if new_pw:
            if len(new_pw) < 1:
                return render_template('account.html', user=user, msg='Password cannot be empty.')
            pw_hash = generate_password_hash(new_pw)
            update_user_password(user['id'], pw_hash)
            msg = ('ID changed.' if msg and 'ID' in msg else 'Password updated.')

        if not msg:
            msg = 'No changes made.'
        user = get_user(user['id'])

    return render_template('account.html', user=user, msg=msg)


# ─── Presets ───

@bp.route('/presets/save', methods=['POST'])
def presets_save():
    user = _login_required()
    label = request.form.get('label', '').strip() or 'Unnamed'
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    create_preset(user['id'], label, name, description)
    return redirect('/LeanAI/profile')


@bp.route('/presets/<int:preset_id>/delete', methods=['POST'])
def presets_delete(preset_id):
    user = _login_required()
    delete_preset(preset_id, user['id'])
    return redirect('/LeanAI/profile')


# ─── Characters ───

@bp.route('/characters/new', methods=['GET', 'POST'])
def character_new():
    user = _login_required()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        personality = request.form.get('personality', '').strip()
        greeting = request.form.get('greeting', '').strip()

        if not name:
            return render_template(
                'character_new.html', user=user,
                msg='Name is required.',
            )

        ignore_global = 1 if request.form.get('ignore_global_prompt') else 0
        char_id = create_character(user['id'], name, personality, greeting, ignore_global)

        file = request.files.get('avatar')
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in ALLOWED_EXT:
                filename = secure_filename(f'char_{char_id}{ext}')
                filepath = os.path.join(AVATAR_DIR, filename)
                file.save(filepath)
                update_character_avatar(char_id, filename)

        return redirect('/LeanAI')

    return render_template('character_new.html', user=user)


# ─── Character actions ───

@bp.route('/characters/<int:char_id>/edit', methods=['GET', 'POST'])
def character_edit(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return redirect('/LeanAI')

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        personality = request.form.get('personality', '').strip()
        greeting = request.form.get('greeting', '').strip()
        if not name:
            return render_template('character_new.html', user=user, char=char, msg='Name is required.')
        ignore_global = 1 if request.form.get('ignore_global_prompt') else 0
        update_character(char_id, user['id'], name, personality, greeting, ignore_global)

        file = request.files.get('avatar')
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in ALLOWED_EXT:
                filename = secure_filename(f'char_{char_id}{ext}')
                filepath = os.path.join(AVATAR_DIR, filename)
                file.save(filepath)
                update_character_avatar(char_id, filename)

        return redirect('/LeanAI')

    return render_template('character_new.html', user=user, char=char)


@bp.route('/characters/<int:char_id>/duplicate', methods=['POST'])
def character_duplicate(char_id):
    user = _login_required()
    new_id = duplicate_character(char_id, user['id'])
    if not new_id:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'ok': True, 'id': new_id})


@bp.route('/characters/<int:char_id>/delete', methods=['POST'])
def character_delete(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not found'}), 404
    avatar_path = char.get('avatar_path', '')
    if avatar_path:
        ap = os.path.join(AVATAR_DIR, os.path.basename(avatar_path))
        if os.path.exists(ap):
            os.remove(ap)
    delete_character(char_id, user['id'])
    return jsonify({'ok': True})


# ─── Chat ───

@bp.route('/characters/<int:char_id>/chat')
def chat(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return redirect('/LeanAI')

    messages = get_character_messages(char_id)
    greeting_shown = None

    if not messages and char.get('greeting'):
        greeting_shown = char['greeting']

    return render_template(
        'chat.html',
        user=user,
        character=char,
        messages=messages,
        greeting_shown=greeting_shown,
        settings=get_ai_settings(user['id']),
    )


@bp.route('/characters/<int:char_id>/chat/send', methods=['POST'])
def chat_send(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Character not found'}), 404

    message = request.form.get('message', '').strip()
    file = request.files.get('file')
    file_path = ''
    file_content = ''

    if file and file.filename:
        fname = secure_filename(f'{user["id"]}_{char_id}_{int(secrets.token_hex(4), 16)}{os.path.splitext(file.filename)[1].lower()}')
        fpath = os.path.join(UPLOAD_DIR, fname)
        file.save(fpath)
        file_path = fname

        ext = os.path.splitext(file.filename)[1].lower()
        if ext in TEXT_EXT:
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    file_content = f.read().strip()
            except Exception:
                file_content = ''

    if not message and not file_content:
        return jsonify({'error': 'Message is empty'}), 400

    full_message = message
    if file_content:
        if full_message:
            full_message += '\n\n--- Attached file content ---\n' + file_content
        else:
            full_message = file_content

    create_message(char_id, 'user', full_message, file_path)

    settings = get_ai_settings(user['id'])
    messages = get_character_messages(char_id)

    system = _build_system_prompt(settings['system_prompt'], char, user)
    lm_messages = _build_lm_messages(system, messages, settings['context_length'], settings['context_messages'])

    reply = _call_lm_studio(
        endpoint=settings['llm_endpoint'],
        model=settings['llm_model'],
        messages=lm_messages,
        temperature=settings['temperature'],
        max_tokens=settings['context_length'],
    )

    if reply is None:
        return jsonify({'error': 'Failed to reach LLM. Check AI Settings.'}), 502

    active_npcs = get_active_npcs(char_id)
    parsed = _parse_npc_response(reply, active_npcs)
    msg_ids = []
    for speaker, content in parsed:
        mid = create_message(char_id, 'assistant', content, speaker=speaker)
        msg_ids.append(mid)
    if not msg_ids:
        mid = create_message(char_id, 'assistant', reply)
        msg_ids.append(mid)
        parsed = [('', reply)]

    reply_messages = [{'speaker': s, 'content': c} for s, c in parsed if c.strip()]
    if not reply_messages:
        reply_messages = [{'speaker': '', 'content': reply}]

    return jsonify({'messages': reply_messages, 'file_path': file_path})


@bp.route('/characters/<int:char_id>/chat/clear', methods=['POST'])
def chat_clear(char_id):
    user = _login_required()
    clear_character_messages(char_id, user['id'])
    clear_memories(char_id)
    save_character_state(char_id, '', '', last_scan_msg_id=0)
    return jsonify({'ok': True})


@bp.route('/characters/<int:char_id>/chat/edit', methods=['POST'])
def chat_edit(char_id):
    user = _login_required()
    data = request.get_json(silent=True) or request.form
    msg_id = int(data.get('message_id'))
    content = data.get('content', '').strip()
    msg = get_message(msg_id, char_id)
    if not msg or msg['character_id'] != char_id:
        return jsonify({'error': 'Message not found'}), 404
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403
    update_message(msg_id, content)
    return jsonify({'ok': True})


@bp.route('/characters/<int:char_id>/chat/delete', methods=['POST'])
def chat_delete(char_id):
    user = _login_required()
    data = request.get_json(silent=True) or request.form
    msg_id = int(data.get('message_id'))
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403
    delete_message(msg_id, char_id)
    return jsonify({'ok': True})


@bp.route('/characters/<int:char_id>/chat/regenerate', methods=['POST'])
def chat_regenerate(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403

    data = request.get_json(silent=True) or request.form
    msg_id = int(data.get('message_id'))
    msg = get_message(msg_id, char_id)
    if not msg or msg['role'] != 'assistant':
        return jsonify({'error': 'Can only regenerate assistant messages'}), 400

    messages = get_character_messages(char_id)
    user_msg = None
    for m in reversed(messages):
        if m['role'] == 'user' and m['id'] < msg_id:
            user_msg = m
            break

    if not user_msg:
        return jsonify({'error': 'No user message to regenerate from'}), 400

    delete_messages_from(msg_id, char_id)

    settings = get_ai_settings(user['id'])
    messages = get_character_messages(char_id)
    system = _build_system_prompt(settings['system_prompt'], char, user)
    lm_messages = _build_lm_messages(system, messages, settings['context_length'], settings['context_messages'])

    reply = _call_lm_studio(
        endpoint=settings['llm_endpoint'],
        model=settings['llm_model'],
        messages=lm_messages,
        temperature=settings['temperature'],
        max_tokens=settings['context_length'],
    )

    if reply is None:
        return jsonify({'error': 'Failed to reach LLM. Check AI Settings.'}), 502

    active_npcs = get_active_npcs(char_id)
    parsed = _parse_npc_response(reply, active_npcs)
    for speaker, content in parsed:
        create_message(char_id, 'assistant', content, speaker=speaker)
    if not parsed:
        create_message(char_id, 'assistant', reply)
        parsed = [('', reply)]

    reply_messages = [{'speaker': s, 'content': c} for s, c in parsed if c.strip()]
    if not reply_messages:
        reply_messages = [{'speaker': '', 'content': reply}]

    return jsonify({'messages': reply_messages})


@bp.route('/characters/<int:char_id>/chat/resend', methods=['POST'])
def chat_resend(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403

    data = request.get_json(silent=True) or request.form
    msg_id = int(data.get('message_id'))
    msg = get_message(msg_id, char_id)
    if not msg or msg['role'] != 'user':
        return jsonify({'error': 'Can only resend user messages'}), 400

    delete_messages_from(msg_id, char_id)

    create_message(char_id, 'user', msg['content'], msg.get('file_path', ''))

    settings = get_ai_settings(user['id'])
    messages = get_character_messages(char_id)
    system = _build_system_prompt(settings['system_prompt'], char, user)
    lm_messages = _build_lm_messages(system, messages, settings['context_length'], settings['context_messages'])

    reply = _call_lm_studio(
        endpoint=settings['llm_endpoint'],
        model=settings['llm_model'],
        messages=lm_messages,
        temperature=settings['temperature'],
        max_tokens=settings['context_length'],
    )

    if reply is None:
        return jsonify({'error': 'Failed to reach LLM. Check AI Settings.'}), 502

    active_npcs = get_active_npcs(char_id)
    parsed = _parse_npc_response(reply, active_npcs)
    for speaker, content in parsed:
        create_message(char_id, 'assistant', content, speaker=speaker)
    if not parsed:
        create_message(char_id, 'assistant', reply)
        parsed = [('', reply)]

    reply_messages = [{'speaker': s, 'content': c} for s, c in parsed if c.strip()]
    if not reply_messages:
        reply_messages = [{'speaker': '', 'content': reply}]

    return jsonify({'messages': reply_messages, 'user_content': msg['content'], 'user_file': msg.get('file_path', '')})


@bp.route('/characters/<int:char_id>/chat/stream', methods=['POST'])
def chat_stream(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Character not found'}), 404

    mode = request.form.get('mode', 'send')
    settings = get_ai_settings(user['id'])

    msg_text = request.form.get('message', '').strip()
    msg_id = int(request.form.get('message_id', 0))
    file = request.files.get('file') if request.files else None
    file_path = ''
    file_content = ''

    if file and file.filename:
        fname = secure_filename(f'{user["id"]}_{char_id}_{int(secrets.token_hex(4), 16)}{os.path.splitext(file.filename)[1].lower()}')
        fpath = os.path.join(UPLOAD_DIR, fname)
        file.save(fpath)
        file_path = fname
        ext = os.path.splitext(file.filename)[1].lower()
        if ext in TEXT_EXT:
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    file_content = f.read().strip()
            except Exception:
                file_content = ''

    def sse_event(event_type, data):
        return f'data: {json.dumps({"type": event_type, **data})}\n\n'

    def generate():
        full_reply = ''
        try:
            if mode == 'send':
                if not msg_text and not file_content:
                    yield sse_event('error', {'error': 'Message is empty'})
                    return

                full_message = msg_text
                if file_content:
                    full_message = (full_message + '\n\n--- Attached file content ---\n' + file_content) if full_message else file_content

                create_message(char_id, 'user', full_message, file_path)

            elif mode == 'regenerate':
                msg = get_message(msg_id, char_id)
                if not msg or msg['role'] != 'assistant':
                    yield sse_event('error', {'error': 'Can only regenerate assistant messages'})
                    return
                messages = get_character_messages(char_id)
                user_msg = None
                for m in reversed(messages):
                    if m['role'] == 'user' and m['id'] < msg_id:
                        user_msg = m
                        break
                if not user_msg:
                    yield sse_event('error', {'error': 'No user message to regenerate from'})
                    return
                delete_messages_from(msg_id, char_id)

            elif mode == 'resend':
                msg = get_message(msg_id, char_id)
                if not msg or msg['role'] != 'user':
                    yield sse_event('error', {'error': 'Can only resend user messages'})
                    return
                delete_messages_from(msg_id, char_id)
                create_message(char_id, 'user', msg['content'], msg.get('file_path', ''))

            else:
                yield sse_event('error', {'error': f'Unknown mode: {mode}'})
                return

            messages = get_character_messages(char_id)
            system = _build_system_prompt(settings['system_prompt'], char, user)
            lm_messages = _build_lm_messages(system, messages, settings['context_length'], settings['context_messages'])

            for chunk in _call_lm_studio_stream(
                endpoint=settings['llm_endpoint'],
                model=settings['llm_model'],
                messages=lm_messages,
                temperature=settings['temperature'],
                max_tokens=settings['context_length'],
            ):
                full_reply += chunk
                yield sse_event('chunk', {'content': chunk})

        except Exception as e:
            yield sse_event('error', {'error': str(e)})
            return

        if not full_reply:
            yield sse_event('error', {'error': 'Failed to reach LLM. Check AI Settings.'})
            return

        active_npcs = get_active_npcs(char_id)
        parsed = _parse_npc_response(full_reply, active_npcs)
        for speaker, content in parsed:
            create_message(char_id, 'assistant', content, speaker=speaker)
        if not parsed:
            create_message(char_id, 'assistant', full_reply)
            parsed = [('', full_reply)]

        reply_messages = [{'speaker': s, 'content': c} for s, c in parsed if c.strip()]
        if not reply_messages:
            reply_messages = [{'speaker': '', 'content': full_reply}]

        yield sse_event('done', {'messages': reply_messages})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ─── Memory & State (robust, model-agnostic) ───

def _process_extraction_payload(char_id, state, existing_memories, data):
    """Save memories/state/npcs from parsed dict. Returns (mem_count, npc_count, state_changed)."""
    mem_count = 0
    npc_count = 0
    state_changed = False

    # memories — de-dup via extraction helper + DB normalized check
    raw_mems = data.get('memories') if isinstance(data.get('memories'), list) else []
    if raw_mems:
        existing_contents = [m['content'] for m in existing_memories] if existing_memories else []
        filtered = _ex.dedup_memories(raw_mems, existing_contents)
        for mem in filtered:
            if add_memory(char_id, mem):
                mem_count += 1

    # npcs
    raw_npcs = data.get('npcs') if isinstance(data.get('npcs'), list) else []
    if raw_npcs:
        for npc in raw_npcs:
            if not isinstance(npc, dict):
                continue
            name = (npc.get('name') or '').strip()
            if not name:
                continue
            # ignore if name equals main char or "User" (case-insensitive)
            add_npc(char_id, name, (npc.get('personality') or '').strip(), (npc.get('relationship') or '').strip())
            npc_count += 1

    # state — location / clothes; accept "keep as is" as no-op
    new_loc = (data.get('location') or '').strip() if isinstance(data.get('location'), str) else ''
    new_clothes = (data.get('clothes') or '').strip() if isinstance(data.get('clothes'), str) else ''
    if new_loc and new_loc.lower() not in ('keep as is', 'unknown', 'not described', 'none'):
        if new_loc != state.get('location', ''):
            state['location'] = new_loc
            state_changed = True
    if new_clothes and new_clothes.lower() not in ('keep as is', 'unknown', 'not described', 'none'):
        if new_clothes != state.get('clothes', ''):
            state['clothes'] = new_clothes
            state_changed = True
    return mem_count, npc_count, state_changed


def _call_extraction(endpoint, model, prompt, max_tokens=800):
    """Single LLM call for extraction with robust parsing. Returns dict."""
    reply = _call_lm_studio(
        endpoint=endpoint,
        model=model,
        messages=[{'role': 'user', 'content': prompt}],
        temperature=0.2,
        max_tokens=max_tokens,
    )
    if reply is None:
        return None, None
    data = _ex.parse_json_robust(reply)
    return data, reply


@bp.route('/characters/<int:char_id>/chat/scan', methods=['POST'])
def chat_scan(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403

    settings = get_ai_settings(user['id'])
    all_msgs = get_character_messages(char_id)

    if len(all_msgs) < 2:
        return jsonify({'ok': True, 'note': 'Need at least 2 messages to scan'})

    state = get_character_state(char_id)
    existing_memories = get_memories(char_id, limit=50)
    npcs = get_character_npcs(char_id)
    existing_npcs = ', '.join([f"{n['name']} ({n['relationship'] or 'no relation'})" for n in npcs]) or 'none'

    # Manual scan: always last 10 (not incremental) so we never miss earlier location/clothes
    window = all_msgs[-10:] if len(all_msgs) > 10 else all_msgs

    prompt = _ex.build_combined_prompt(window, char['name'], state, existing_npcs, existing_memories)

    data, raw = _call_extraction(settings['llm_endpoint'], settings.get('llm_model', ''), prompt, max_tokens=1000)

    if data is None:
        # LLM down: try heuristic only (still advances cursor so UI not stuck)
        h_loc, h_clothes = _ex.heuristic_extract_state(window, char['name'])
        tmp = {}
        if h_loc:
            tmp['location'] = h_loc
        if h_clothes:
            tmp['clothes'] = h_clothes
        if tmp:
            _process_extraction_payload(char_id, state, existing_memories, tmp)
            latest_id = max(int(m['id']) for m in window) if window else 0
            save_character_state(char_id, state.get('location', ''), state.get('clothes', ''), last_scan_msg_id=latest_id)
            return jsonify({'ok': True, 'memories': 0, 'npcs': 0, 'heuristic': True})
        return jsonify({'ok': False, 'error': 'Could not reach LLM. Check your AI Settings.'}), 502

    # If combined yielded nothing and we have at least some content, try focused memories fallback
    if not data or (not data.get('memories') and not data.get('npcs') and data.get('location', '').lower() in ('', 'keep as is')):
        # Fallback: memories-only prompt (more focused for weak models)
        fb_prompt = _ex.build_memories_prompt(window, char['name'], existing_memories)
        fb_data, _ = _call_extraction(settings['llm_endpoint'], settings.get('llm_model', ''), fb_prompt, max_tokens=600)
        if fb_data and fb_data.get('memories'):
            # merge fallback memories into data
            if not data:
                data = {}
            data['memories'] = fb_data.get('memories', [])
            # keep original location/clothes/npcs if not set
            for k in ('location', 'clothes', 'npcs'):
                if k not in data and k in fb_data:
                    data[k] = fb_data[k]

    # Dedicated state fallback: if location/clothes still keep as is, try state-only prompt
    loc_keep = not data.get('location') or str(data.get('location', '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none')
    clothes_keep = not data.get('clothes') or str(data.get('clothes', '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none')
    if loc_keep or clothes_keep:
        s_prompt = _ex.build_state_prompt(window, char['name'], state)
        s_data, _ = _call_extraction(settings['llm_endpoint'], settings.get('llm_model', ''), s_prompt, max_tokens=400)
        if s_data:
            for k in ('location', 'clothes'):
                v = s_data.get(k)
                if v and str(v).strip().lower() not in ('', 'keep as is', 'unknown', 'not described', 'none'):
                    # only override if still keep as is or empty
                    if not data.get(k) or str(data.get(k, '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none'):
                        data[k] = v

    # Heuristic deterministic fallback (model-independent) — fills gaps when LLM still says keep as is
    h_loc, h_clothes = _ex.heuristic_extract_state(window, char['name'])
    if h_loc and (not data.get('location') or str(data.get('location', '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none')):
        data['location'] = h_loc
    if h_clothes and (not data.get('clothes') or str(data.get('clothes', '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none')):
        data['clothes'] = h_clothes
        # also consider merging if LLM gave single item but heuristic has more
        if data.get('clothes') and h_clothes and h_clothes.lower() not in data.get('clothes','').lower():
            # merge heuristic items not already present
            existing = [c.strip().lower() for c in str(data.get('clothes','')).split(',')]
            for hc in [c.strip() for c in h_clothes.split(',')]:
                if hc.lower() not in existing and hc.lower() not in ('', 'keep as is'):
                    data['clothes'] = data['clothes'] + ', ' + hc

    if not data:
        data = {}

    mem_count, npc_count, changed = _process_extraction_payload(char_id, state, existing_memories, data)

    # Always advance last_scan_msg_id to latest overall (so auto interval is incremental after manual)
    latest_id = max(int(m['id']) for m in all_msgs) if all_msgs else 0
    save_character_state(char_id, state.get('location', ''), state.get('clothes', ''), last_scan_msg_id=latest_id)

    return jsonify({'ok': True, 'memories': mem_count, 'npcs': npc_count})


@bp.route('/characters/<int:char_id>/chat/extract', methods=['POST'])
def chat_extract(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403

    settings = get_ai_settings(user['id'])
    interval = settings.get('auto_extract_interval', 10)
    # interval semantics: "every X messages" -> every X NEW messages since last_scan
    # 0 = disabled (UI slider 0)
    if not interval or int(interval) == 0:
        return jsonify({'ok': True, 'note': 'Auto-extract disabled'})

    interval = int(interval)
    all_messages = get_character_messages(char_id)
    state = get_character_state(char_id)

    should, note = _ex.should_auto_extract(all_messages, state, interval)
    if not should:
        return jsonify({'ok': True, 'note': note})

    # Build incremental window: only new messages since last_scan, pad to 4-10 for context
    window = _ex.get_window_since_last_scan(all_messages, state, max_window=10, min_window=4)
    if len(window) < 2:
        return jsonify({'ok': True, 'note': 'No new messages to extract'})

    existing_memories = get_memories(char_id, limit=50)
    npcs = get_character_npcs(char_id)
    existing_npcs = ', '.join([f"{n['name']} ({n['relationship'] or 'no relation'})" for n in npcs]) or 'none'

    prompt = _ex.build_combined_prompt(window, char['name'], state, existing_npcs, existing_memories)

    data, raw = _call_extraction(settings['llm_endpoint'], settings.get('llm_model', ''), prompt, max_tokens=800)

    if data is None:
        # LLM unreachable — try heuristic only, still advance so we don't stall forever
        h_loc, h_clothes = _ex.heuristic_extract_state(window, char['name'])
        if h_loc or h_clothes:
            tmp = {}
            if h_loc:
                tmp['location'] = h_loc
            if h_clothes:
                tmp['clothes'] = h_clothes
            _process_extraction_payload(char_id, state, existing_memories, tmp)
            latest_id = max(int(m['id']) for m in window) if window else 0
            save_character_state(char_id, state.get('location', ''), state.get('clothes', ''), last_scan_msg_id=latest_id)
            return jsonify({'ok': True, 'heuristic': True})
        return jsonify({'ok': False, 'error': 'Could not reach LLM'}), 502

    # Fallback for weak models that return empty combined but have extractable memories
    if not data or (not data.get('memories') and len(window) >= 2):
        # Heuristic pre-filter: if window contains likely fact keywords, try focused prompt
        joined = " ".join(m.get('content','') for m in window).lower()
        has_signal = any(k in joined for k in ('my name', 'i am', 'i love', 'i like', 'remember', 'we ', 'moved', 'born', 'work as', 'live in', 'sister', 'brother', 'mother', 'father', 'office', 'wearing', 'blouse', 'blazer'))
        if has_signal or not data:
            fb_prompt = _ex.build_memories_prompt(window, char['name'], existing_memories)
            fb_data, _ = _call_extraction(settings['llm_endpoint'], settings.get('llm_model', ''), fb_prompt, max_tokens=500)
            if fb_data and fb_data.get('memories'):
                if not data:
                    data = {}
                data['memories'] = fb_data.get('memories', [])

    # Dedicated state fallback: always ensure location/clothes are attempted
    loc_keep = not data.get('location') or str(data.get('location', '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none')
    clothes_keep = not data.get('clothes') or str(data.get('clothes', '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none')
    if loc_keep or clothes_keep:
        s_prompt = _ex.build_state_prompt(window, char['name'], state)
        s_data, _ = _call_extraction(settings['llm_endpoint'], settings.get('llm_model', ''), s_prompt, max_tokens=400)
        if s_data:
            for k in ('location', 'clothes'):
                v = s_data.get(k)
                if v and str(v).strip().lower() not in ('', 'keep as is', 'unknown', 'not described', 'none'):
                    if not data.get(k) or str(data.get(k, '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none'):
                        data[k] = v

    # Heuristic deterministic fallback — final safety net
    h_loc, h_clothes = _ex.heuristic_extract_state(window, char['name'])
    if h_loc and (not data.get('location') or str(data.get('location', '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none')):
        data['location'] = h_loc
    if h_clothes and (not data.get('clothes') or str(data.get('clothes', '')).strip().lower() in ('', 'keep as is', 'unknown', 'not described', 'none')):
        data['clothes'] = h_clothes
    elif h_clothes and data.get('clothes') and h_clothes.lower() not in str(data.get('clothes','')).lower():
        # merge additional items not already present
        existing = [c.strip().lower() for c in str(data.get('clothes','')).split(',')]
        for hc in [c.strip() for c in h_clothes.split(',')]:
            if hc.lower() not in existing and hc.lower() not in ('', 'keep as is'):
                data['clothes'] = data['clothes'] + ', ' + hc

    if not data:
        data = {}

    # Save (uses normalized dedup inside)
    _process_extraction_payload(char_id, state, existing_memories, data)

    # Advance cursor regardless of whether we found something — we processed window
    latest_id = max(int(m['id']) for m in window) if window else 0
    save_character_state(char_id, state.get('location', ''), state.get('clothes', ''), last_scan_msg_id=latest_id)

    return jsonify({'ok': True})


@bp.route('/characters/<int:char_id>/chat/memories', methods=['GET'])
def chat_memories(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403
    memories = get_memories(char_id)
    return jsonify({'memories': memories})


@bp.route('/characters/<int:char_id>/chat/memories/clear', methods=['POST'])
def chat_memories_clear(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403
    clear_memories(char_id)
    return jsonify({'ok': True})


@bp.route('/characters/<int:char_id>/chat/state', methods=['GET'])
def chat_state(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403
    state = get_character_state(char_id)
    return jsonify({'state': state})


# ─── NPCs ───

@bp.route('/characters/<int:char_id>/npcs', methods=['GET'])
def chat_npcs(char_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403
    npcs = get_character_npcs(char_id)
    return jsonify({'npcs': npcs})


@bp.route('/characters/<int:char_id>/npcs/<int:npc_id>/toggle', methods=['POST'])
def chat_npc_toggle(char_id, npc_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403
    data = request.get_json(silent=True) or request.form
    is_active = int(data.get('is_active', 1))
    set_npc_active(npc_id, char_id, is_active)
    return jsonify({'ok': True})


@bp.route('/characters/<int:char_id>/npcs/<int:npc_id>/delete', methods=['POST'])
def chat_npc_delete(char_id, npc_id):
    user = _login_required()
    char = get_character(char_id, user['id'])
    if not char:
        return jsonify({'error': 'Not authorized'}), 403
    delete_npc(npc_id, char_id)
    return jsonify({'ok': True})


# ─── AI Settings ───

@bp.route('/settings/ai', methods=['GET', 'POST'])
def settings_ai():
    user = _login_required()
    if not user:
        return redirect('/LeanAI')
    msg = None
    models = []

    if request.method == 'POST':
        context_length = int(request.form.get('context_length', 1024))
        temperature = float(request.form.get('temperature', 0.7))
        system_prompt = request.form.get('system_prompt', '').strip()
        fmt_stripped = NPC_FORMAT.strip()
        if system_prompt.endswith(fmt_stripped):
            system_prompt = system_prompt[:-len(fmt_stripped)].rstrip()
        llm_endpoint = request.form.get('llm_endpoint', '').strip().rstrip('/')
        llm_model = request.form.get('llm_model', '').strip()
        context_messages = int(request.form.get('context_messages', 50))
        auto_extract_interval = int(request.form.get('auto_extract_interval', 10))
        auto_extract = 0 if auto_extract_interval == 0 else 1
        streaming = 1 if request.form.get('streaming') else 0
        stream_speed = int(request.form.get('stream_speed', 0))

        save_ai_settings(user['id'], context_length, temperature, system_prompt, llm_endpoint, llm_model, context_messages, auto_extract, auto_extract_interval, streaming, stream_speed)
        msg = 'AI settings saved.'

        if llm_endpoint:
            try:
                req = urllib.request.Request(llm_endpoint + '/models')
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    models = data.get('data', [])
            except Exception:
                pass

    settings = get_ai_settings(user['id'])
    display_prompt = (settings['system_prompt'] + NPC_FORMAT) if settings['system_prompt'] else NPC_FORMAT.lstrip()

    if settings['llm_endpoint'] and not models:
        try:
            req = urllib.request.Request(settings['llm_endpoint'] + '/models')
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                models = data.get('data', [])
        except Exception:
            pass

    return render_template(
        'ai_settings.html',
        user=user,
        settings=settings,
        display_prompt=display_prompt,
        models=models,
        msg=msg,
    )


# ─── Image Generation ───

@bp.route('/image-gen')
def image_gen():
    user = _login_required()
    if not user:
        return redirect('/LeanAI/')
    history = get_image_history(user['id'])
    return render_template('image_gen.html', user=user, history=history)


@bp.route('/image-gen/models')
def image_gen_models():
    user = _login_required()
    if not user:
        return jsonify({'error': 'Login required'}), 401
    models = comfyui_list_models()
    try:
        req = urllib.request.Request(COMFYUI_URL + '/object_info/VAELoader')
        with urllib.request.urlopen(req, timeout=5) as resp:
            info = json.loads(resp.read())
        vaes = info.get('VAELoader', {}).get('input', {}).get('required', {}).get('vae_name', [[]])[0]
    except Exception:
        vaes = []
    schedulers = ['karras', 'exponential', 'normal', 'simple', 'sgm_uniform', 'ddim_uniform', 'beta']
    try:
        req = urllib.request.Request(COMFYUI_URL + '/object_info/LoraLoader')
        with urllib.request.urlopen(req, timeout=5) as resp:
            info = json.loads(resp.read())
        loras = info.get('LoraLoader', {}).get('input', {}).get('required', {}).get('lora_name', [[]])[0]
    except Exception:
        loras = []
    model_info = {}
    for m in models:
        lower = m.lower()
        is_z = 'z_image' in lower or 'z-image' in lower
        is_xl = any(k in lower for k in ('xl', 'pony', 'illustrious', 'noob', 'sdxl', 'flux', 'realvis', 'juggernaut', 'hassaku'))
        # SD1.5 vs SDXL detection for resolution hint
        model_info[m] = {'sdxl': is_xl or is_z, 'z_image': is_z, 'is_z': is_z}
    # also probe for upscale models to inform hires quality
    upscale_models = []
    try:
        req = urllib.request.Request(COMFYUI_URL + '/object_info/UpscaleModelLoader')
        with urllib.request.urlopen(req, timeout=5) as resp:
            info = json.loads(resp.read())
        upscale_models = info.get('UpscaleModelLoader', {}).get('input', {}).get('required', {}).get('model_name', [[]])[0]
    except Exception:
        pass
    return jsonify({'models': models, 'vaes': vaes, 'schedulers': schedulers, 'loras': loras, 'model_info': model_info, 'upscale_models': upscale_models})


@bp.route('/image-gen/generate', methods=['POST'])
def image_gen_generate():
    user = _login_required()
    if not user:
        return jsonify({'error': 'Login required'}), 401

    data = request.get_json(silent=True) or {}
    prompt = data.get('prompt', '').strip()
    negative = data.get('negative', '').strip()
    model = data.get('model', '')
    width = int(data.get('width', 1024))
    height = int(data.get('height', 1024))
    steps = int(data.get('steps', 28))
    cfg = int(data.get('cfg', 7))
    seed = int(data.get('seed', -1))
    sampler = data.get('sampler', 'dpmpp_2m_sde')
    scheduler = data.get('scheduler', 'karras')
    vae = data.get('vae', '')
    hires_fix = bool(data.get('hires_fix', False))
    raw_loras = data.get('loras', [])
    loras = []
    for rl in raw_loras:
        if isinstance(rl, dict) and rl.get('name'):
            loras.append({
                'name': rl['name'],
                'strength_model': float(rl.get('strength_model', 1.0)),
                'strength_clip': float(rl.get('strength_clip', 1.0)),
            })

    if not prompt:
        return jsonify({'error': 'Prompt is required'}), 400
    if not model:
        return jsonify({'error': 'No model selected'}), 400

    if seed < 0:
        seed = int.from_bytes(os.urandom(4), 'big') % (2**32)

    workflow_data = comfyui_build_workflow(prompt, negative, model, width, height, steps, cfg, seed, sampler, scheduler, vae, hires_fix, loras)
    timeout = 240 if hires_fix else 120
    result = comfyui_queue_and_wait(workflow_data, timeout=timeout)
    if not result:
        return jsonify({'error': 'ComfyUI timed out or returned no image'}), 502

    image_data = comfyui_get_image(result['filename'], result.get('subfolder', ''), result.get('type', 'output'))

    uploads_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)
    fname = f'img_{int(time.time())}_{seed}.png'
    fpath = os.path.join(uploads_dir, fname)
    with open(fpath, 'wb') as f:
        f.write(image_data)

    settings_json = json.dumps({
        'width': width, 'height': height, 'steps': steps,
        'cfg': cfg, 'seed': seed, 'sampler': sampler,
    })
    add_generated_image(user['id'], prompt, negative, model, settings_json, fname)

    return jsonify({'ok': True, 'image': f'/LeanAI/static/uploads/{fname}', 'seed': seed})


@bp.route('/image-gen/history')
def image_gen_history():
    user = _login_required()
    if not user:
        return jsonify({'error': 'Login required'}), 401
    history = get_image_history(user['id'])
    return jsonify({'history': history})


@bp.route('/image-gen/<int:image_id>/delete', methods=['POST'])
def image_gen_delete(image_id):
    user = _login_required()
    if not user:
        return jsonify({'error': 'Login required'}), 401
    delete_generated_image(image_id, user['id'])
    return jsonify({'ok': True})


@bp.route('/image-gen/queue')
def image_gen_queue():
    user = _login_required()
    if not user:
        return jsonify({'error': 'Login required'}), 401
    try:
        req = urllib.request.Request(COMFYUI_URL + '/queue')
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        running = len(data.get('queue_running', []))
        pending = len(data.get('queue_pending', []))
        return jsonify({'running': running, 'pending': pending, 'queue_depth': running + pending})
    except Exception:
        return jsonify({'running': -1, 'pending': -1, 'queue_depth': -1})


# ─── Helpers ───

def _build_system_prompt(system_prompt, char, user):
    parts = []

    main_parts = []
    if system_prompt and not char.get('ignore_global_prompt'):
        main_parts.append(system_prompt)
    main_parts.append(NPC_FORMAT.lstrip())
    parts.append('\n\n'.join(main_parts))

    parts.append(f"Character: {char['personality']}")
    parts.append(f"You are {char['name']}. Respond as {char['name']} would, in character at all times. Never break character.")
    if char.get('greeting'):
        parts.append(f"Your opening greeting: {char['greeting']}")
    if user['name']:
        parts.append(f"User info: {user['name']} - {user['description']}" if user['description'] else f"User info: {user['name']}")

    state = get_character_state(char['id'])
    if state.get('location') or state.get('clothes'):
        state_lines = []
        if state.get('location'):
            state_lines.append(f"- Location: {state['location']}")
        if state.get('clothes'):
            state_lines.append(f"- You are wearing: {state['clothes']}")
        if state_lines:
            parts.append("Current state:\n" + '\n'.join(state_lines))

    memories = get_memories(char['id'], limit=8)
    if memories:
        mem_lines = [f"• {m['content']}" for m in memories]
        parts.append("Recent memories:\n" + '\n'.join(mem_lines))

    active_npcs = get_active_npcs(char['id'])
    if active_npcs:
        npc_lines = []
        for n in active_npcs:
            desc = n['name']
            if n.get('relationship'):
                desc += f" ({n['relationship']})"
            if n.get('personality'):
                desc += f": {n['personality']}"
            npc_lines.append('- ' + desc)
        parts.append("Active NPCs:\n" + '\n'.join(npc_lines))

    return '\n\n'.join(parts)


def _build_lm_messages(system, messages, max_tokens, max_messages=50):
    lm = [{'role': 'system', 'content': system}]
    est_used = len(system) // 4

    messages = messages[-max_messages:]

    normalized = []
    for m in messages:
        role = 'assistant' if m['role'] == 'assistant' else 'user'
        content = m['content']
        if m['role'] == 'assistant' and m.get('speaker', ''):
            content = f"[{m['speaker']:}] {content}"
        if normalized and normalized[-1]['role'] == role:
            normalized[-1]['content'] += '\n\n' + content
        else:
            normalized.append({'role': role, 'content': content})

    while normalized and normalized[0]['role'] == 'assistant':
        normalized.pop(0)

    selected = []
    for m in reversed(normalized):
        est_used += len(m['content']) // 4 + 10
        if est_used > max_tokens:
            break
        selected.append(m)

    selected.reverse()
    while selected and selected[0]['role'] == 'assistant':
        selected.pop(0)
    lm.extend(selected)

    return lm


def _parse_npc_response(response, active_npcs):
    if not response or not active_npcs:
        return [('', response or '')]
    npc_names = [n['name'] for n in active_npcs]
    escaped = [re.escape(n) for n in npc_names]
    pattern = re.compile(r'^\[(' + '|'.join(escaped) + r')\]:\s*', re.MULTILINE)
    lines = response.split('\n')
    result = []
    current_speaker = ''
    current_lines = []
    for line in lines:
        match = pattern.match(line)
        if match:
            if current_lines:
                result.append((current_speaker, '\n'.join(current_lines).strip()))
            current_speaker = match.group(1)
            rest = line[match.end():]
            current_lines = [rest] if rest else []
        else:
            current_lines.append(line)
    if current_lines or current_speaker != '':
        result.append((current_speaker, '\n'.join(current_lines).strip()))
    result = [(s, c) for s, c in result if c.strip()]
    return result if result else [('', response.strip())]


def _call_lm_studio(endpoint, model, messages, temperature, max_tokens):
    body = {
        'messages': messages,
        'temperature': temperature,
        'max_tokens': min(max_tokens, 2000),
    }
    if model:
        body['model'] = model

    try:
        req = urllib.request.Request(
            endpoint + '/chat/completions',
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data['choices'][0]['message']['content']
    except Exception as e:
        print(f'LM Studio error: {e}')
        return None


def _call_lm_studio_stream(endpoint, model, messages, temperature, max_tokens):
    body = {
        'messages': messages,
        'temperature': temperature,
        'max_tokens': min(max_tokens, 2000),
        'stream': True,
    }
    if model:
        body['model'] = model

    req = urllib.request.Request(
        endpoint + '/chat/completions',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    resp = urllib.request.urlopen(req, timeout=120)
    try:
        for line in resp:
            line = line.decode('utf-8', errors='replace').strip()
            if not line.startswith('data: '):
                continue
            payload = line[6:]
            if payload == '[DONE]':
                break
            try:
                chunk = json.loads(payload)
                delta = chunk['choices'][0].get('delta', {})
                content = delta.get('content')
                if content:
                    yield content
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
    finally:
        resp.close()


# ─── ComfyUI Helpers ───

COMFYUI_URL = 'http://localhost:8188'


def _is_z_image_model(name: str) -> bool:
    low = (name or '').lower()
    return 'z_image' in low or 'z-image' in low or 'z_image_turbo' in low


def comfyui_list_models():
    models = []
    # CheckpointLoaderSimple (SD1.5/SDXL)
    try:
        req = urllib.request.Request(COMFYUI_URL + '/object_info/CheckpointLoaderSimple')
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        ckpts = data.get('CheckpointLoaderSimple', {}).get('input', {}).get('required', {}).get('ckpt_name', [[]])[0]
        models.extend(ckpts)
    except Exception as e:
        print(f'ComfyUI list models error (ckpt): {e}')
    # UNETLoader (Z-Image Turbo, Flux, etc diffusion_models)
    try:
        req = urllib.request.Request(COMFYUI_URL + '/object_info/UNETLoader')
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        unets = data.get('UNETLoader', {}).get('input', {}).get('required', {}).get('unet_name', [[]])[0]
        models.extend(unets)
    except Exception as e:
        print(f'ComfyUI list models error (unet): {e}')
    # dedup preserve order
    seen = set()
    uniq = []
    for m in models:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def comfyui_build_workflow(prompt, negative, model, width, height, steps, cfg, seed,
                           sampler, scheduler='karras', vae='', hires_fix=False, loras=None):
    client_id = str(uuid.uuid4())

    # ── Z-Image Turbo branch (UNETLoader + CLIPLoader lumina2) ──
    if _is_z_image_model(model):
        # Z-Image expects: UNETLoader + CLIPLoader(qwen) + EmptySD3LatentImage + ModelSamplingAuraFlow + KSampler(res_multistep/simple)
        workflow = {
            '30': {
                'class_type': 'CLIPLoader',
                'inputs': {'clip_name': 'qwen_3_4b.safetensors', 'type': 'lumina2', 'device': 'default'},
            },
            '29': {
                'class_type': 'VAELoader',
                'inputs': {'vae_name': vae if vae and vae != 'pixel_space' else 'ae.safetensors'},
            },
            '28': {
                'class_type': 'UNETLoader',
                'inputs': {'unet_name': model, 'weight_dtype': 'default'},
            },
            '13': {
                'class_type': 'EmptySD3LatentImage',
                'inputs': {'width': width, 'height': height, 'batch_size': 1},
            },
        }
        # pixel_space VAE alias -> keep 29 as above, but if user explicitly wants pixel_space
        if vae == 'pixel_space':
            workflow['29']['inputs']['vae_name'] = 'pixel_space'

        model_ref = ['28', 0]
        clip_ref = ['30', 0]
        if loras:
            for i, lora in enumerate(loras):
                node_id = str(20 + i)
                workflow[node_id] = {
                    'class_type': 'LoraLoader',
                    'inputs': {
                        'lora_name': lora['name'],
                        'strength_model': lora.get('strength_model', 1.0),
                        'strength_clip': lora.get('strength_clip', 1.0),
                        'model': model_ref,
                        'clip': clip_ref,
                    },
                }
                model_ref = [node_id, 0]
                clip_ref = [node_id, 1]

        workflow['27'] = {
            'class_type': 'CLIPTextEncode',
            'inputs': {'text': prompt, 'clip': clip_ref},
        }
        workflow['33'] = {
            'class_type': 'ConditioningZeroOut',
            'inputs': {'conditioning': ['27', 0]},
        }
        workflow['11'] = {
            'class_type': 'ModelSamplingAuraFlow',
            'inputs': {'shift': 3.0, 'model': model_ref},
        }
        # Z-Image turbo optimal: 8 steps cfg 1 res_multistep simple
        z_steps = max(4, min(int(steps), 12)) if steps else 8
        # force cfg 1 for turbo (higher breaks color)
        workflow['3'] = {
            'class_type': 'KSampler',
            'inputs': {
                'seed': seed,
                'steps': z_steps,
                'cfg': 1.0,
                'sampler_name': 'res_multistep' if sampler not in ('res_multistep', 'euler', 'dpm++_2m') else sampler,
                'scheduler': 'simple' if scheduler not in ('simple', 'beta', 'normal') else scheduler,
                'denoise': 1.0,
                'model': ['11', 0],
                'positive': ['27', 0],
                'negative': ['33', 0],
                'latent_image': ['13', 0],
            },
        }
        vae_source = ['29', 0]
        if hires_fix:
            workflow['15'] = {
                'class_type': 'LatentUpscale',
                'inputs': {
                    'upscale_method': 'bislerp',
                    'width': width * 2,
                    'height': height * 2,
                    'crop': 'disabled',
                    'samples': ['3', 0],
                },
            }
            hires_seed = int.from_bytes(os.urandom(4), 'big') % (2**32)
            workflow['12'] = {
                'class_type': 'KSampler',
                'inputs': {
                    'seed': hires_seed,
                    'steps': z_steps,
                    'cfg': 1.0,
                    'sampler_name': workflow['3']['inputs']['sampler_name'],
                    'scheduler': workflow['3']['inputs']['scheduler'],
                    'denoise': 0.55,
                    'model': ['11', 0],
                    'positive': ['27', 0],
                    'negative': ['33', 0],
                    'latent_image': ['15', 0],
                },
            }
            workflow['10'] = {
                'class_type': 'VAEDecode',
                'inputs': {'samples': ['12', 0], 'vae': vae_source},
            }
            workflow['9'] = {
                'class_type': 'SaveImage',
                'inputs': {'filename_prefix': 'leanai', 'images': ['10', 0]},
            }
        else:
            workflow['8'] = {
                'class_type': 'VAEDecode',
                'inputs': {'samples': ['3', 0], 'vae': vae_source},
            }
            workflow['9'] = {
                'class_type': 'SaveImage',
                'inputs': {'filename_prefix': 'leanai', 'images': ['8', 0]},
            }
        return {'prompt': workflow, 'client_id': client_id}

    # ── SD1.5 / SDXL branch (CheckpointLoaderSimple) ──
    workflow = {
        '4': {
            'class_type': 'CheckpointLoaderSimple',
            'inputs': {'ckpt_name': model},
        },
        '5': {
            'class_type': 'EmptyLatentImage',
            'inputs': {'width': width, 'height': height, 'batch_size': 1},
        },
    }

    model_ref = ['4', 0]
    clip_ref = ['4', 1]
    if loras:
        for i, lora in enumerate(loras):
            node_id = str(20 + i)
            workflow[node_id] = {
                'class_type': 'LoraLoader',
                'inputs': {
                    'lora_name': lora['name'],
                    'strength_model': lora.get('strength_model', 1.0),
                    'strength_clip': lora.get('strength_clip', 1.0),
                    'model': model_ref,
                    'clip': clip_ref,
                },
            }
            model_ref = [node_id, 0]
            clip_ref = [node_id, 1]

    workflow['6'] = {
        'class_type': 'CLIPTextEncode',
        'inputs': {'text': prompt, 'clip': clip_ref},
    }
    workflow['7'] = {
        'class_type': 'CLIPTextEncode',
        'inputs': {'text': negative, 'clip': clip_ref},
    }
    workflow['3'] = {
        'class_type': 'KSampler',
        'inputs': {
            'seed': seed,
            'steps': steps,
            'cfg': cfg,
            'sampler_name': sampler,
            'scheduler': scheduler,
            'denoise': 1.0,
            'model': model_ref,
            'positive': ['6', 0],
            'negative': ['7', 0],
            'latent_image': ['5', 0],
        },
    }

    vae_source = ['4', 2]
    if vae:
        workflow['10'] = {
            'class_type': 'VAELoader',
            'inputs': {'vae_name': vae},
        }
        vae_source = ['10', 0]

    if hires_fix:
        # Prefer high-quality latent upscale (bislerp) — far better than nearest-exact
        # If upscale_models are installed, frontend could opt for UltimateSDUpscale; backend keeps latent path for compatibility
        workflow['11'] = {
            'class_type': 'LatentUpscale',
            'inputs': {
                'upscale_method': 'bislerp',
                'width': width * 2,
                'height': height * 2,
                'crop': 'disabled',
                'samples': ['3', 0],
            },
        }
        hires_seed = int.from_bytes(os.urandom(4), 'big') % (2**32)
        workflow['12'] = {
            'class_type': 'KSampler',
            'inputs': {
                'seed': hires_seed,
                'steps': steps,
                'cfg': cfg,
                'sampler_name': sampler,
                'scheduler': scheduler,
                'denoise': 0.55,
                'model': model_ref,
                'positive': ['6', 0],
                'negative': ['7', 0],
                'latent_image': ['11', 0],
            },
        }
        workflow['13'] = {
            'class_type': 'VAEDecode',
            'inputs': {'samples': ['12', 0], 'vae': vae_source},
        }
        workflow['9'] = {
            'class_type': 'SaveImage',
            'inputs': {'filename_prefix': 'leanai', 'images': ['13', 0]},
        }
    else:
        workflow['8'] = {
            'class_type': 'VAEDecode',
            'inputs': {'samples': ['3', 0], 'vae': vae_source},
        }
        workflow['9'] = {
            'class_type': 'SaveImage',
            'inputs': {'filename_prefix': 'leanai', 'images': ['8', 0]},
        }

    return {'prompt': workflow, 'client_id': client_id}


def comfyui_queue_and_wait(workflow_data, timeout=120):
    body = json.dumps(workflow_data).encode()
    req = urllib.request.Request(
        COMFYUI_URL + '/prompt',
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())

    prompt_id = result.get('prompt_id')
    if not prompt_id:
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(COMFYUI_URL + f'/history/{prompt_id}')
            with urllib.request.urlopen(req, timeout=5) as resp:
                history = json.loads(resp.read())
            if prompt_id in history:
                outputs = history[prompt_id].get('outputs', {})
                for node_out in outputs.values():
                    images = node_out.get('images', [])
                    if images:
                        return images[0]
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    return None


def comfyui_get_image(filename, subfolder='', img_type='output'):
    url = f'{COMFYUI_URL}/view?filename={filename}&subfolder={subfolder}&type={img_type}'
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()
