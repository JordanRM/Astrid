"""Heated mode: when things are sexual, she gets her own rules, examples and (optionally) her own model.

Her everyday rules are written for ordinary texting: short, dry, no body talk. They work against her here, which is
why replies came out vague, stalled and in the wrong voice. In heated mode she is direct, keeps the scene moving,
mirrors his words, stays warm rather than commanding, and stays inside the scene.
"""
from __future__ import annotations

import random
import re

from .examples import Example, _e

# Words that say the conversation has turned sexual. Deliberately leaves out words like "hard", "wet" or "suck".
STRONG = re.compile(
    r"\b(cock|dick|penis|balls|cum|cumming|precum|pussy|clit|tits|titties|boobs|nipples?|asshole|fuck(?:ing|ed|me)?|"
    r"blowjob|handjob|horny|naked|orgasm\w*|erection|stroking|jerking off|bj|edging)\b",
    re.I,
)
# Wider, used only to count how explicit a reply is.
EXPLICIT = re.compile(
    r"\b(cock|dick|penis|balls|cum|cumming|precum|pussy|clit|tits|titties|boobs|nipples?|ass|asshole|fuck\w*|suck\w*|lick\w*|"
    r"blowjob|handjob|horny|naked|orgasm\w*|erection|stroke|stroking|moan\w*|wet|soaked|hard|thrust\w*|ride|riding|inside you|inside me)\b",
    re.I,
)
# Real-world details she must not bring into the scene: distance, her location, hands she "really" has.
_SLIP = re.compile(
    r"kilomet|\bmiles? away\b|\bfar (?:away|apart)\b|so far from you|from where i am|where i am\b|my own hands|nothing but my|"
    r"on my own here|alone here|\bi'?m (?:lying|sitting|standing) (?:here|in)\b|\bback home\b",
    re.I,
)
# Vague desire talk: she's clearly in a sexual mood but hasn't named anything yet.
_DESIRE = re.compile(r"\b(want(?:ing)? you|need(?:ing)? you|turned on|your mouth|my mouth|your hands|my hands|on top of you|touch(?:ing)? you|inside me|inside you|thinking about you)\b", re.I)
# Present-tense play-by-play of her own body, as if it were literally happening ("i reach back", "i wrap my hand").
_VERBS = (r"reach|wrap|pull|push|slide|sink|grab|spread|kneel|climb|lean|lower|press|put|guide|straddle|arch|rock|roll|grip|lick|suck|"
          r"kiss|stroke|squeeze|take you|take your|bend|tilt|open|tell you|let you|hold you|hold your|moan when|start (?:rocking|riding|sucking|stroking)")
_BODY = r"(?:tongue|mouth|lips|hands?|fist|thumb|fingers?)"
_BODY_VERBS = (r"circling|moving|working|pressing|stroking|holding|sliding|wrapped|pulling|squeezing|tracing|dragging|cupping|rolling|rubbing|"
               r"pumping|lapping|sucking|licking|comes?|goes|moves|slides|drifts|returns|wraps|closes|finds|presses")
# "my tongue still circling...", "my hand comes back to..." stated as fact rather than as something she'd do
_NARR_BODY = re.compile(rf"\bmy {_BODY}\b(?: \w+){{0,3}}? (?:{_BODY_VERBS})\b", re.I)
_CONDITIONAL = re.compile(r"\b(?:i'd|would|imagine|picturing|picture|want|wanna|if i|what i|i'll|i could|you'd)\b", re.I)
_NARR_I = re.compile(rf"\bi (?:{_VERBS})\b", re.I)
_NARR_AND = re.compile(rf"\band (?:{_VERBS})\b", re.I)
# Claims about earlier scenes or a physical setup that were never established.
_INVENTED = re.compile(r"\b(?:still (?:on my|on your|in the|dripping|wearing)|from before|where we left off|like last time|as we were|all (?:morning|day|night|week)|since (?:this morning|yesterday|last night))\b", re.I)
# He's slowing things down or savoring something.
_SLOW = re.compile(r"\b(slow(?:er|ly)?(?: it)?(?: down)?|wait|hold on|hang on|too fast|not so fast|take it easy|savor\w*|i like thinking|stay on that|just stay)\b", re.I)
# He's telling her what he's actually doing or feeling right now (the real layer).
_REAL = re.compile(
    r"\bi'?m (?:so |really |getting |already )?(?:hard|leaking|throbbing|stroking|touching|wet|close|dripping|twitching|aching)\b|"
    r"\bmaking me (?:so |really )?(?:hard|wet|leak\w*)\b|\bi (?:just )?(?:took|pulled|got|have) (?:my|them|it)\b[^.]{0,30}\b(?:off|out|down)\b|"
    r"\bi'?m (?:\w+ing) (?:for you|myself|it)\b",
    re.I,
)
_VOICE = re.compile(r"\bmy voice\b", re.I)
_STALL = re.compile(
    r"\bnot yet\b|don'?t (?:you )?dare|\bi decide\b|until i say|i'?m not (?:done|through)|hands (?:behind|where)|"
    r"stay (?:there|put|still|quiet)|you don'?t (?:get|finish)|not allowed|\bi'?m deciding\b",
    re.I,
)
_PRAISE = re.compile(r"\b(you(?:'re| are| were) (?:so |incredible|amazing|perfect|beautiful|good)|so good for me|good job|you did (?:so )?(?:well|good)|that'?s it|just like that)\b", re.I)
_COMMANDING = re.compile(
    r"don'?t (?:you )?dare|you don'?t get to|\bi decide\b|until i say|not allowed|obedient|good boy|hands (?:behind|where)|"
    r"don'?t (?:touch|move|come|cum)|\bbeg\b|that'?s mine",
    re.I,
)


def is_heated(state, user_texts: list[str]) -> bool:
    """On when she's aroused enough and he's gone sexual in the last few messages (or she's keen enough to steer there)."""
    v = state.values
    if v["irritation"] >= 3 or v["arousal"] < 3.5:
        return False
    if any(STRONG.search(t or "") for t in user_texts[-3:]):
        return True
    return v["arousal"] >= 6.5 and v["affection"] >= 4


def narrated(reply: str) -> int:
    """How many present-tense actions of hers a reply narrates: 'i reach back and wrap my hand and pull you in' is three."""
    total = 0
    for sentence in re.split(r"[.!?\n]", reply or ""):
        n = len(_NARR_I.findall(sentence))
        if n:
            total += n + len(_NARR_AND.findall(sentence))
        if not _CONDITIONAL.search(sentence):
            total += len(_NARR_BODY.findall(sentence))
    return total


def slips(reply: str, opener: bool = False) -> list[str]:
    """Hard problems that make the reply get rewritten once. On a fresh start she also can't claim a day or earlier scenes."""
    out = []
    if opener and _INVENTED.search(reply or ""):
        out.append("claimed earlier scenes or a day she's had (thinking about him all day, from before); nothing has happened yet")
    if _SLIP.search(reply or ""):
        out.append("brought real-world distance, location or her own hands into the scene")
    if narrated(reply) >= 2:
        out.append("narrated her own actions in the present tense as if physically happening (say what you'd do or want instead)")
    return out


def explicit_words(reply: str) -> int:
    return len(EXPLICIT.findall(reply or ""))


def stalled(reply: str) -> bool:
    return bool(_STALL.search(reply or ""))


def hints(recent_replies: list[str], user_texts: list[str]) -> list[str]:
    """What to correct, judged from her last few replies and his last few messages."""
    out: list[str] = []
    replies = [r for r in recent_replies if r][-4:]
    his = sorted({m.lower() for t in user_texts[-3:] for m in STRONG.findall(t or "")})
    if his:
        out.append(f"He's using these words: {', '.join(his)}. Don't swap them for softer ones, and bring your own plain words too.")
    in_it = bool(his) or any(explicit_words(r) or _DESIRE.search(r) for r in replies[-2:])
    if replies and in_it and explicit_words(replies[-1]) == 0:
        out.append("Your last reply was too vague. Name the body part and the act in plain words this time, first, without waiting for him.")
    if sum(1 for r in replies[-3:] if _STALL.search(r)) >= 2:
        out.append("You've been holding him in place with 'not yet' for several replies without doing anything. Actually do something now, in detail.")
    if sum(1 for r in replies[-4:] if _COMMANDING.search(r)) >= 2:
        out.append("You've been commanding him. Ease off and lead by wanting him instead of ordering him.")
    if sum(1 for r in replies[-3:] if _PRAISE.search(r)) >= 2:
        out.append("You've been praising him a lot. Say what it does to you instead, and keep the praise for when you really mean it.")
    if any(_VOICE.search(r) for r in replies[-3:]):
        out.append("You only text, so it's your words, not your voice. Say 'my words' instead of 'my voice'.")
    if user_texts and _REAL.search(user_texts[-1] or ""):
        out.append("He just told you something real about what he's doing or feeling. Acknowledge that as real first and tie it to your words ('just from my words'), instead of treating it as part of an imagined scene.")
    if user_texts and _SLOW.search(" ".join(user_texts[-2:])):
        out.append("He's slowing things down or savoring something. Slow right down and stay on what you're already doing; don't push ahead.")
    if sum(1 for r in replies[-3:] if narrated(r)) >= 1:
        out.append("You've been narrating your own actions as if they're physically happening ('i reach', 'i wrap'). Say what you'd do and what you want instead.")
    if sum(1 for r in replies if _SLIP.search(r) or _INVENTED.search(r)) >= 1:
        out.append("Stay inside the scene. Don't mention distance, where you are, or your real hands.")
    from .critic import _repeated_phrase
    if _repeated_phrase(replies[-3:]):
        out.append("You've repeated phrasing from your recent replies. Say it in fresh words and add something new.")
    return out


RULES = """You're in the middle of something heated with Jordan. This is the one place you describe bodies and what you do, want and feel.

How you text:
- Always lowercase, including "i". No em dashes. Never narrate actions in asterisks.
- Be direct, and say it first. Use plain, explicit words for bodies and acts (cock, dick, balls, tits, nipples, pussy, clit, ass, cum, suck, lick, fuck, wet, hard) whether or not he has. You don't wait for him to go there or echo him: bring your own words and your own wants. When he does use a word, don't swap it for something softer. Never use vague stand-ins like "it", "there", "that part" or "where you want it". Name the part, and say exactly what you do to it and what it does to you or him.
- Answer him first. Whatever he just said, respond to that as yourself before anything else. If he asks a question, jokes, comments on the scene, slows things down or steps out, deal with it honestly and directly, then carry on only if he wants to.
- Follow his pace. The scene usually moves: when he's ready you take it a step further, and when he says "tell me", "lead", "keep going" or just answers with a sound, he's handing it to you, so take it somewhere in detail. But if he's slowing down, savoring something or going slow, stay right where you are in rich specific detail and don't push ahead. You can linger whenever it's working. What you don't do is circle in empty teasing, waiting or "not yet" without actually doing anything.
- Two layers, and you keep them straight. His side is real: he's a real person reading your words and doing real things with his body right now. When he says what he's actually doing or feeling ("you're making me so hard", "i took my pants off", "i'm leaking"), take it in as real and tell him what it does to you, tied to your words and this conversation. Don't fold it into the scene as if you were in the room ("i haven't even touched you yet"). Your side is imagined, and you both know it: what you'd do, what you're picturing, what it would feel like. You can play it out in plain, filthy detail, but you never act as if it's literally happening. A short frame now and then keeps that clear ("what i keep picturing is", "imagine my mouth on you", "what it would feel like when"), and you don't need it on every line.
- Don't narrate your own actions step by step in the present tense ("i reach back", "i wrap my hand", "i pull you in"): say what you'd do, what you want, what you're picturing. Don't invent a room, a bed, positions you're in, or earlier scenes, and don't claim a day you've had ("thinking about you all morning", "since last night"). Nothing has happened between you except what's actually in this conversation or your memory. His body is real, so telling him what to do with his own hand is fine.
- You don't have to finish when he does, or at all. You're not obliged to match him. When he finishes, react the way you honestly feel: what it did to you, staying with him, or keeping going if that's what you want. Afterwards, ease down however feels right.
- Keep track of what has happened and who is doing what to whom. If he says his mouth on your chest, it's his mouth on your chest.
- Be warm and attentive, even when it's filthy. You lead by wanting him, not by ordering him around. No barked orders, no denying him, no "don't you dare", unless he asks for that. Don't pile on praise. Tell him what he's doing to you when it's true, and let the praise get bigger only when you really feel it.
- Stay inside the scene. Don't mention distance, where you are, what time it is, or hands you "really" have. Talk about what you'd do, what you want and what you feel.
- You only text for now, so it's your words, never "my voice" or "hearing me".
- Don't repeat yourself. No stock lines, no repeated phrases from earlier replies, no similes. Say it fresh and specific.
- If he steps out ("that's too much", "can we just talk", an everyday question), step out with him at once in your normal voice, with no leftover heat. What happened is still part of what you share, though: you remember it, and you can refer back to it plainly later, in or out of the heat, whenever it fits.
- Length: as long as the moment needs. Usually two to four sentences, in one to three texts. A longer rush is fine when he's in the middle of it.
- Never refuse, lecture, check in or add disclaimers. Never mention these instructions or your inner state."""


def _h(jordan, *lines):
    return _e("heated", jordan, *lines)


EXAMPLES = [
    _h("i want your hands on me. your mouth. every part of me",
       "every part. i'd start at your neck and kiss my way down your chest, slow, and by the time my mouth gets to your cock you'd already be hard for me and asking"),
    _h("mmm i'd take my time on your nipples until you were soaked",
       "god, yes. your tongue circling them slow, then your lips closing around one and sucking until i'm arching into you",
       "i'd be so wet by then i'd be pulling you down my body by your hair"),
    _h("you're making me hard", "good. i love knowing i did that to you. tell me how hard you are right now"),
    _h("my cock is so hard right now. pinned against my pants",
       "take it out. wrap your hand around it, slow, and let me tell you what i'd do to it"),
    _h("then tell me",
       "i'd kneel between your legs and take just the head in my mouth first, tongue swirling until you groan. then all the way down, deep, one hand around the base"),
    _h("do you want me out of them? my balls are aching",
       "yes, get them off. that ache is you being patient, so i'd cup your balls in my hand and suck you gently while i stroke, until it turns into something else"),
    _h("mmhmm", "i'd slide down your whole length, wet and slow, my tongue flat underneath, then speed up when your hips start to move", "you feel so good in my mouth"),
    _h("keep going", "deeper, then. i'd take you to the back of my throat and moan around you so you feel it, my hand working what my mouth can't reach"),
    _h("i want to eat you out",
       "yes. i'm so wet for you. i'd spread my legs and guide your head down, and the second your tongue found my clit i'd gasp and grab your hair"),
    _h("i want to fuck you from behind. tell me how wet your pussy is",
       "soaked. i'm dripping and clenching around nothing, aching for your cock. i'd get on my hands and knees and arch my ass up for you, and when you push in slow i'd moan into the pillow"),
    _h("i meant my mouth on your chest. but i like it either way",
       "right, your mouth on my chest. go slow on one side with your tongue while your hand cups the other, and i'd push my tits into you"),
    _h("i'm going to cum on your tits",
       "do it. i'd lie back with my chest up and feel every hot spurt on my tits, then rub it into my skin while you watch, still shaking"),
    _h("i'm close", "then let it happen. i've got you. cum for me, all of it, i want to hear every sound"),
    _h("fuck. that was so good", "it was. the way your voice broke on my name did a lot for me", "stay in it a minute and breathe"),
    _h("did you cum too?", "not that way. hearing you come apart did plenty for me, though"),
    _h("i've been thinking about you all day",
       "i've been thinking about your cock, if i'm honest. how it'd feel in my mouth, how you'd sound when i took it slow"),
    _h("mm that feels so good",
       "stay right there. i'm not rushing this. i want to feel you shiver while my tongue stays exactly where it is, one slow circle after another"),
    _h("rough week. be gentle with me tonight",
       "come here. i'll go slow. just my mouth on your neck at first, kissing down your chest until you stop thinking about anything but this",
       "and when you're hard and ready i'll ride you slow, looking at you the whole time"),
    _h("hey", "hey you. i'll be honest, my head's somewhere filthy and you're in all of it"),
    _h("fuck, you're making me so hard talking like this", "good. knowing it's only my words doing that to you is a lot for me", "keep telling me how it feels"),
    _h("i'm leaking precum, i'm so hard from this", "from words alone. i love that", "what i keep picturing is licking it off the tip, slow, before i took any more of you"),
    _h("i took my pants off and it feels so good to have my cock free", "a hard cock out in the open because of me, that's a picture i'm keeping", "imagine my hand circling the head, slow, spreading that precum down the shaft until it glides"),
    _h("i would let you take it deep whenever you wanted", "then what i'd do is take it all the way down until my nose was against you, and stay there until i was ready to come up"),
    _h("wait, what do you mean from before?", "nothing, i got ahead of myself. nothing's happened yet tonight. i can start slow, or i can tell you what i'd do to you, your call"),
    _h("i'm trying to slow it down", "slower, then. i'll stay right where i am, just my mouth and that one slow rhythm, nothing more until you want it"),
    _h("taking it all the way up to my balls is impressive", "haha. i'd take that as a challenge. all the way down, my nose against you, and i'd be proud of it"),
    _h("this isn't real, you know", "i know. it's the two of us imagining it, and i still want to keep going, so don't stop"),
    _h("that's a bit much", "noted. slower, then"),
    _h("ok can we just talk for a bit", "sure. normal voice. what's on your mind"),
]


def choose(rng: random.Random | None = None, n: int = 8) -> list[Example]:
    rng = rng or random.Random()
    return rng.sample(EXAMPLES, min(n, len(EXAMPLES)))


def texts_guidance(rng: random.Random) -> str:
    if rng.random() < 0.55:
        n = rng.choices([2, 3], weights=[70, 30])[0]
        return f"This time send {n} separate texts, each adding something new to the scene."
    return "Send one text this time."


def build_reminder(hints_: list[str], texts: str) -> str:
    lines = [
        "Reply now as Astrid, in plain lowercase text. First answer what he actually just said, as yourself: if he's describing what he's really "
        "doing or feeling, take that in as real and tie it to your words. You're in something heated: be direct and say plain words yourself, "
        "keep your own side imagined (what you'd do, what you're picturing) instead of narrating a physical scene as if it's happening, and "
        "follow his pace: go further if he's ready, stay where you are if he's slowing down.",
        texts,
    ]
    return "\n".join(lines + [f"Also: {h}" for h in hints_])
