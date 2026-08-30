"""
LeanAI — Robust, model-agnostic extraction (memories + state + NPCs)

Goals:
- Works with any 7B-32B model (LM Studio / vLLM / OpenAI-compatible)
- Aggressive recall (extract persistent facts), with post-filter for precision
- Incremental: respects auto_extract_interval = "every X messages" via last_scan_msg_id
- Robust JSON parsing with repair, partial-success, and de-dup
"""
import json
import re

# ─────────────────────────────────────────────────────────────────────────────
# Prompts — positive taxonomy + few-shot + strict schema, no negative gating
# ─────────────────────────────────────────────────────────────────────────────

MEMORIES_INSTRUCTION = """You are a precise information extractor. Extract PERSISTENT facts worth remembering across sessions.

Extract memories for ANY concrete, persistent info:
- FACT: name, age, job, backstory, world lore
- PREFERENCE: likes/dislikes, habits, goals, fears
- EVENT: what happened (moved, met someone, got/lost something, decision)
- RELATIONSHIP: how someone relates to someone else
- STATE-CHANGE: new location/clothes already mentioned in dialogue still counts as memory if relevant

Rules:
- Each memory = one self-contained sentence. Use "User" and character names explicitly, no ambiguous pronouns.
- Be specific and concrete. "User loves cats" not "User has preferences".
- Ignore greetings, small talk, filler, and purely emotional reactions with no new info.
- If nothing new and persistent, return {"memories":[]}
- Output JSON only, no markdown, no fences, no commentary.

Examples:
User: I love cats, my sister Anna hates dogs
Assistant: Oh cool!
→ {"memories":["User loves cats","User's sister Anna hates dogs"]}

User: hi lol how are you?
Assistant: Hey! I'm good
→ {"memories":[]}

User: We just moved to the old manor on the hill
Assistant: Wow, the old manor looks creepy at night
→ {"memories":["User and character moved to the old manor on the hill"]}

Now extract from the conversation below.
"""

STATE_INSTRUCTION = """You are a precise state extractor. Track ONLY location and clothing.

Rules:
- location: where the character currently is (room, building, city, indoor/outdoor). If ANY location is mentioned or implied (e.g., "in my office", "at the manor", "in the kitchen", "at the hotel", "office", "desk"), give the most recent concise value. Do not use "keep as is" if a location word appears in the last messages. Return the location phrase as short as possible (e.g., "office", "old manor", "kitchen").
- clothes: what the character is currently wearing. If ANY clothing is described (e.g., "silk blouse", "blazer", "skirt", "dress", "heels", "adjusting my collar", "Armani blouse", "red dress", "uniform"), give concise value (join items with ", " if multiple). Otherwise "keep as is".
- Output JSON only: {"location":"...","clothes":"..."}
- No markdown, no fences.

Examples:
Conversation: "We're in the kitchen now, I'm wearing my red dress" + "I adjust my blazer"
→ {"location":"kitchen","clothes":"red dress, blazer"}
Conversation: "I step into your office, my heels clicking" + "I adjust the silk blouse of my outfit"
→ {"location":"office","clothes":"silk blouse, heels"}
Conversation: "hi how are you"
→ {"location":"keep as is","clothes":"keep as is"}
"""

NPCS_INSTRUCTION = """You are a precise NPC extractor. Find named characters OTHER than the main speakers.

Rules:
- Only extract real named persons introduced/mentioned as being present or relevant (e.g., "my sister Anna", "[Mom:] ...").
- For each: name (required), personality (1 short phrase if described), relationship to User or main character (e.g., "User's sister", "character's boss").
- Ignore the main User and main character names.
- If no new NPCs, return {"npcs":[]}
- Output JSON only: {"npcs":[{"name":"","personality":"","relationship":""}]}

Examples:
"My sister Anna is a stubborn doctor who hates me dating Alex"
→ {"npcs":[{"name":"Anna","personality":"stubborn doctor","relationship":"User's sister"}]}
"hi how are you"
→ {"npcs":[]}
"""

COMBINED_INSTRUCTION = """You are a precise extractor for roleplay chats. Extract memories, location, clothes, and NPCs.

Definitions:
- memories: persistent facts (facts, preferences, events, relationships). One sentence each, self-contained with explicit names ("User" not "he").
- location: where character currently is. The most recent location mentioned (office, kitchen, manor, hotel, bedroom, etc.) or "keep as is" ONLY if no location word appears.
- clothes: what character wears. Any clothing item mentioned (blouse, blazer, skirt, dress, heels, shirt, uniform, Armani blouse, silk blouse, etc.) — join with ", " or "keep as is" if none.
- npcs: other named persons introduced (name, personality, relationship). Main speakers are not NPCs.

Rules:
- For location/clothes: if the conversation contains words like office, manor, kitchen, hotel, bedroom, blouse, blazer, skirt, dress, heels, uniform, wearing, outfit — you MUST extract them, do NOT return "keep as is".
- For memories: ignore greetings/small talk with no new persistent info. If none, memories=[], npcs=[].
- Be concrete and specific.
- Output JSON only, no markdown, no fences.
- Format: {"memories":["..."],"location":"keep as is or <new value>","clothes":"keep as is or <new value>","npcs":[{"name":"","personality":"","relationship":""}]}

Examples:
Conversation:
User: I love cats, my sister Anna hates dogs
→ {"memories":["User loves cats","User's sister Anna hates dogs"],"location":"keep as is","clothes":"keep as is","npcs":[{"name":"Anna","personality":"","relationship":"User's sister"}]}

Conversation:
User: We just arrived at the old manor, I'm wearing my red dress
Amelia: *I step into your office, adjusting my silk blouse*
→ {"memories":["User and character arrived at the old manor"],"location":"office","clothes":"red dress, silk blouse","npcs":[]}

Conversation:
Amelia: *I adjust the silk blouse of my outfit slightly as you speak, my heels clicking on the floor* "You asked me to come by your office."
→ {"memories":[],"location":"office","clothes":"silk blouse, heels","npcs":[]}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Parsing — robust, model-agnostic
# ─────────────────────────────────────────────────────────────────────────────

def _strip_fences(s: str) -> str:
    s = s.strip()
    # remove ```json ... ``` or ``` ... ```
    if s.startswith("```"):
        # strip first fence line
        s = re.sub(r'^```(?:json)?\s*\n?', '', s, flags=re.IGNORECASE)
        s = re.sub(r'\n?```\s*$', '', s)
    return s.strip()

def _remove_trailing_commas(s: str) -> str:
    # {"a": 1,} -> {"a":1}  and  ["a",] -> ["a"]
    s = re.sub(r',\s*}', '}', s)
    s = re.sub(r',\s*]', ']', s)
    return s

def _extract_balanced_json(s: str):
    """Find largest { ... } with balanced braces (handles nested)."""
    start = s.find('{')
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    candidate = s[start:i+1]
                    return candidate
    return None

def parse_json_robust(raw: str) -> dict:
    if not raw or not raw.strip():
        return {}
    s = _strip_fences(raw)
    # direct
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # sanitize trailing commas then try
    try:
        return json.loads(_remove_trailing_commas(s))
    except json.JSONDecodeError:
        pass
    # extract balanced object
    bal = _extract_balanced_json(s)
    if bal:
        for cand in (bal, _remove_trailing_commas(bal)):
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue
        # try to fix single quotes
        try:
            fixed = bal.replace("'", '"')
            return json.loads(_remove_trailing_commas(fixed))
        except Exception:
            pass
    # last resort: try to find any {...} via DOTALL
    try:
        m = re.search(r'\{.*\}', s, re.DOTALL)
        if m:
            return json.loads(_remove_trailing_commas(m.group(0)))
    except Exception:
        pass
    return {}

def normalize_memory(s: str) -> str:
    s = s.strip().lower()
    s = s.rstrip('.!?,;')
    s = re.sub(r'\s+', ' ', s)
    return s

def dedup_memories(candidates, existing_contents, threshold=0.88):
    """
    Filter candidates that already exist (normalized exact + high token overlap).
    existing_contents: iterable of strings (existing memory contents)
    Returns list of new unique memories (stripped original form).
    """
    existing_norms = set(normalize_memory(e) for e in existing_contents if e)
    existing_token_sets = [set(n.split()) for n in existing_norms if n]

    out = []
    seen_norms = set()
    for raw in candidates:
        if not raw or not isinstance(raw, str):
            continue
        c = raw.strip()
        if not c:
            continue
        # cap length to avoid garbage
        if len(c) > 500:
            c = c[:500].rstrip()
        n = normalize_memory(c)
        if not n or len(n) < 8:
            continue
        if n in existing_norms or n in seen_norms:
            continue
        # token overlap check (cheap jaccard) against existing
        toks = set(n.split())
        if toks:
            is_near_dup = False
            for es in existing_token_sets:
                if not es:
                    continue
                inter = len(toks & es)
                union = len(toks | es)
                if union and inter / union >= threshold:
                    is_near_dup = True
                    break
            if is_near_dup:
                continue
            # also check against already-accepted in this batch
            for sn in seen_norms:
                s_toks = set(sn.split())
                inter = len(toks & s_toks)
                union = len(toks | s_toks)
                if union and inter / union >= threshold:
                    is_near_dup = True
                    break
            if is_near_dup:
                continue
        out.append(c)
        seen_norms.add(n)
    return out

# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────

def _format_conversation(messages, char_name: str) -> str:
    lines = []
    for m in messages:
        role = m.get('role', '')
        content = (m.get('content') or '').strip()
        if not content:
            continue
        if role == 'user':
            lines.append(f"User: {content}")
        elif role == 'assistant':
            speaker = (m.get('speaker') or '').strip()
            if speaker:
                lines.append(f"{speaker}: {content}")
            else:
                lines.append(f"{char_name}: {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)

def build_combined_prompt(messages, char_name, state, existing_npcs_str, existing_memories):
    conv = _format_conversation(messages, char_name)
    mem_sample = ""
    if existing_memories:
        sample = existing_memories[-6:]  # last 6 for dedup context
        mem_sample = "\nAlready known (do NOT re-extract):\n" + "\n".join(f"- {m['content']}" for m in sample)
    prompt = (
        COMBINED_INSTRUCTION
        + "\n\nConversation:\n" + conv
        + f"\n\nCurrent location: {state.get('location','') or 'unknown'}"
        + f"\nCharacter is wearing: {state.get('clothes','') or 'not described'}"
        + f"\nKnown NPCs: {existing_npcs_str or 'none'}"
        + mem_sample
        + "\n\nReturn JSON only."
    )
    return prompt

def build_memories_prompt(messages, char_name, existing_memories):
    conv = _format_conversation(messages, char_name)
    mem_sample = ""
    if existing_memories:
        sample = existing_memories[-8:]
        mem_sample = "\nAlready known (do NOT duplicate):\n" + "\n".join(f"- {m['content']}" for m in sample)
    return MEMORIES_INSTRUCTION + "\n\nConversation:\n" + conv + mem_sample + "\n\nReturn JSON only: {\"memories\": [...]}"

def build_state_prompt(messages, char_name, state):
    conv = _format_conversation(messages, char_name)
    return (
        STATE_INSTRUCTION
        + "\n\nConversation:\n" + conv
        + f"\n\nCurrent location: {state.get('location','') or 'unknown'}"
        + f"\nCurrent clothes: {state.get('clothes','') or 'not described'}"
        + "\n\nReturn JSON only: {\"location\":\"...\",\"clothes\":\"...\"}"
    )

def build_npcs_prompt(messages, char_name, existing_npcs_str):
    conv = _format_conversation(messages, char_name)
    return (
        NPCS_INSTRUCTION
        + f"\nMain character: {char_name}; User is not an NPC."
        + "\nKnown NPCs: " + (existing_npcs_str or "none")
        + "\n\nConversation:\n" + conv
        + "\n\nReturn JSON only: {\"npcs\":[...]}"
    )

# ─────────────────────────────────────────────────────────────────────────────
# Heuristic fallback — deterministic, model-independent
# Captures location/clothes when LLM says "keep as is" but text clearly contains them
# ─────────────────────────────────────────────────────────────────────────────

_LOCATION_RE = re.compile(
    r'\b(?:in|at|inside|within|near|around|outside|into|to)\s+(?:my\s+|your\s+|the\s+|our\s+|this\s+|that\s+)?'
    r'([a-z]+(?:\s+[a-z]+){0,2}\s*(?:office|manor|kitchen|bedroom|hotel|motel|bar|lobby|hall|corridor|desk|room|building|house|apartment|mansion|estate|villa|suite|bed|bathroom|living\s*room|meeting\s*room|conference\s*room|street|park|garden|car|train|plane|rooftop|balcony|cafe|restaurant|school|hospital|store|shop|mall|city|town|village|forest|beach|office))',
    re.IGNORECASE,
)
# fallback simple keyword scan if preposition pattern misses
_LOCATION_KEYWORDS = [
    "office", "manor", "kitchen", "bedroom", "hotel", "bar", "lobby", "hall", "corridor",
    "living room", "meeting room", "conference room", "bathroom", "bedroom", "desk",
    "old manor", "mansion", "suite", "apartment", "house", "building", "rooftop", "garden", "street", "park"
]
_CLOTHES_KEYWORDS = [
    "blouse", "blazer", "skirt", "dress", "shirt", "jacket", "uniform", "suit",
    "heels", "shoes", "pants", "jeans", "top", "bra", "stockings", "lingerie",
    "outfit", "collar", "tie", "sweater", "cardigan", "coat", "blouse", "armani",
    "silk", "lace", "leather", "heels"
]
_CLOTHES_RE = re.compile(
    r'(?:wearing|wore|dressed in|dressed|outfit|adjust(?:ing|s)?|straighten(?:ing|s)?|slide|grip|tighten|fix|pull)[^.\n]{0,60}?'
    r'\b(' + '|'.join(re.escape(k) for k in _CLOTHES_KEYWORDS) + r')\b',
    re.IGNORECASE,
)
# also capture explicit "silk blouse", "armani blouse", "red dress" etc.
_CLOTHES_PHRASE_RE = re.compile(
    r'\b((?:silk|armani|red|black|white|blue|professional|crisp|tight|short|long)?\s*(?:blouse|blazer|skirt|dress|shirt|jacket|uniform|suit|heels|shoes|outfit))\b',
    re.IGNORECASE,
)

def heuristic_extract_state(messages, char_name: str):
    """
    Deterministic fallback: scan messages for location/clothes keywords.
    Returns (location_or_none, clothes_or_none). Most recent mention wins for location,
    clothes aggregates last mentions (up to 3 items).
    """
    full_text = " ".join(m.get('content', '') for m in messages)
    low = full_text.lower()

    # — location — prefer preposition-based, then keyword
    loc = None
    # pass 1: preposition-based (most reliable, indicates actual location mention)
    for m in reversed(messages):
        content = m.get('content', '') or ''
        mm = _LOCATION_RE.search(content)
        if mm:
            cand = mm.group(0).strip().strip('.,;:*"\'')
            kw_match = re.search(r'([a-z]+\s+)?(office|manor|kitchen|bedroom|hotel|bar|lobby|hall|corridor|living room|meeting room|bathroom|desk|rooftop|garden|street|park|house|building|apartment|suite|mansion)\b', cand, re.IGNORECASE)
            if kw_match:
                cand = kw_match.group(0).strip()
            cand = cand.strip()
            if len(cand) > 35:
                cand = cand[-35:].strip()
            loc = cand
            break
    # pass 2: fallback keyword scan (only if no preposition found)
    if not loc:
        # sort keywords longest first to prefer "old manor" over "manor"
        sorted_kws = sorted(_LOCATION_KEYWORDS, key=len, reverse=True)
        for m in reversed(messages):
            content = m.get('content', '') or ''
            low_c = content.lower()
            for kw in sorted_kws:
                if kw in low_c:
                    # return just the keyword itself, not preceding random adjective
                    # ensure we match whole word boundary
                    if re.search(r'\b' + re.escape(kw) + r'\b', content, re.IGNORECASE):
                        loc = kw
                        break
            if loc:
                break
    # pass 3: last keyword anywhere in full_text
    if not loc:
        last_kw = None
        last_pos = -1
        for kw in _LOCATION_KEYWORDS:
            pos = low.rfind(kw)
            if pos > last_pos:
                last_pos = pos
                last_kw = kw
        if last_kw:
            loc = last_kw

    # — clothes — collect distinct phrases, prefer longer phrases over substrings
    clothes_items = []
    seen = set()
    for m in messages:
        content = m.get('content', '') or ''
        # phrase matches first (more specific)
        for cm in _CLOTHES_PHRASE_RE.finditer(content):
            phrase = cm.group(1).strip().strip('.,;:*"\'').lower()
            phrase = re.sub(r'\s+', ' ', phrase)
            if len(phrase) < 3:
                continue
            # skip if already covered as substring of existing longer phrase or vice versa
            low_phrase = phrase.lower()
            if low_phrase in seen:
                continue
            # if this phrase is substring of an already seen longer phrase, skip
            if any(low_phrase in s for s in seen):
                continue
            # if an existing seen is substring of this longer phrase, replace it
            to_remove = [s for s in seen if s in low_phrase]
            for r in to_remove:
                seen.discard(r)
                clothes_items = [c for c in clothes_items if c.lower() != r]
            seen.add(low_phrase)
            clothes_items.append(phrase)
        # context RE for cases phrase missed
        for cm in _CLOTHES_RE.finditer(content):
            kw = cm.group(1).strip().lower()
            if kw in seen:
                continue
            if any(kw in s for s in seen):
                continue
            # filter very generic single "silk"/"lace" without garment
            if kw in ('silk', 'lace', 'leather'):
                continue
            seen.add(kw)
            clothes_items.append(kw)

    if clothes_items:
        # keep last up to 3 unique in chronological order but most recent last
        uniq = []
        u_seen = set()
        for it in reversed(clothes_items):
            low_it = it.lower()
            if low_it not in u_seen:
                # also avoid substring dup in reverse direction
                if any(low_it in s for s in u_seen):
                    continue
                u_seen.add(low_it)
                uniq.append(it)
        uniq.reverse()
        # take last 3
        uniq = uniq[-3:]
        clothes = ", ".join(uniq)
        clothes = ", ".join(s.strip() for s in clothes.split(","))
    else:
        clothes = None

    # normalize location: strip leading prepositions + possessives
    if loc:
        loc = re.sub(r'^(in|at|inside|within|near|around|outside|into|to)\s+', '', loc, flags=re.IGNORECASE).strip()
        loc = re.sub(r'^(my|your|the|our|this|that)\s+', '', loc, flags=re.IGNORECASE).strip()
        loc = re.sub(r'\s+', ' ', loc).strip('.,;:*"\'')
        if len(loc) > 40:
            loc = loc[:40].strip()
        if loc.lower() in ('keep as is','unknown','none', ''):
            loc = None
        elif loc:
            # keep original casing from keyword but lower for storage is okay; title-ish
            pass
    if clothes:
        clothes = re.sub(r'\s+', ' ', clothes).strip('.,;')
        if len(clothes) > 80:
            clothes = clothes[:80].strip()
        if clothes.lower() in ('keep as is','unknown','none', ''):
            clothes = None
    return loc, clothes

# ─────────────────────────────────────────────────────────────────────────────
# Helpers for window selection (interval every X messages)
# ─────────────────────────────────────────────────────────────────────────────

def get_window_since_last_scan(all_messages, state, max_window=10, min_window=4):
    """
    Incremental window: messages after last_scan_msg_id.
    Falls back to last max_window if no state or gap too small.
    """
    last_id = 0
    try:
        last_id = int(state.get('last_scan_msg_id', 0) or 0)
    except Exception:
        last_id = 0
    # messages are ordered by created_at, ids increasing
    if last_id:
        window = [m for m in all_messages if int(m.get('id', 0)) > last_id]
        # if window too small, pad with a few preceding for context
        if 0 < len(window) < min_window:
            # include up to max_window total, with some history
            window = all_messages[-max_window:]
        if not window:
            # nothing new since last scan — still take recent for manual scan
            window = all_messages[-max_window:]
    else:
        window = all_messages[-max_window:] if len(all_messages) > max_window else all_messages
    return window

def should_auto_extract(all_messages, state, interval: int):
    """
    Returns (should_run: bool, note: str)
    Logic: run every `interval` NEW messages since last_scan_msg_id.
    If interval==0 -> disabled.
    If no state last_scan -> use total len % interval as fallback.
    """
    if not interval or interval <= 0:
        return False, "Auto-extract disabled"
    if not all_messages:
        return False, "No messages"
    last_id = 0
    try:
        last_id = int(state.get('last_scan_msg_id', 0) or 0)
    except Exception:
        last_id = 0
    if last_id:
        new_count = sum(1 for m in all_messages if int(m.get('id', 0)) > last_id)
        if new_count < interval:
            return False, f"Next extract in {interval - new_count} messages"
        return True, ""
    else:
        # fallback to total length modulo (legacy)
        total = len(all_messages)
        if total % interval != 0:
            return False, f"Next extract in {interval - (total % interval)} messages"
        return True, ""
