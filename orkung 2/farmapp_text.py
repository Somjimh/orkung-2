"""Words for the farm app in English and Kiswahili, and the farm's areas.

Screen words live here so a worker can switch the whole app to Kiswahili.
Job titles carry their own English and Kiswahili text (routines.title_en /
title_sw), so they are not listed here.
"""
from flask import session, g

LANGS = ("en", "sw")

AREAS = [
    # key,        English,          Kiswahili,            colour,    text colour
    ("livestock", "Livestock",      "Mifugo",             "#7A4A23", "#FFFFFF"),
    ("apiary",    "Apiary",         "Nyuki",              "#F2B33D", "#2A1A00"),
    ("bananas",   "Bananas",        "Ndizi",              "#F5D547", "#2B2400"),
    ("fodder",    "Fodder",         "Malisho",            "#2F7D32", "#FFFFFF"),
    ("nursery",   "Nurseries",      "Kitalu",             "#B7DD8C", "#1C3305"),
    ("water",     "Water & pipes",  "Maji na mabomba",    "#1565C0", "#FFFFFF"),
    ("spraying",  "Spraying",       "Kunyunyizia dawa",   "#C62828", "#FFFFFF"),
    ("general",   "Other work",     "Kazi nyingine",      "#6A1B9A", "#FFFFFF"),
]
AREA = {a[0]: dict(key=a[0], en=a[1], sw=a[2], bg=a[3], fg=a[4]) for a in AREAS}

WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "sw": ["Jumatatu", "Jumanne", "Jumatano", "Alhamisi", "Ijumaa", "Jumamosi", "Jumapili"],
}
MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"],
    "sw": ["Januari", "Februari", "Machi", "Aprili", "Mei", "Juni", "Julai", "Agosti", "Septemba",
           "Oktoba", "Novemba", "Desemba"],
}

WORDS = {
    "home": ("Home", "Nyumbani"),
    "back": ("Back", "Rudi"),
    "hello": ("Hello", "Habari"),
    "my_jobs": ("My jobs today", "Kazi zangu leo"),
    "jobs_for": ("Jobs for", "Kazi za"),
    "jobs_today": ("Jobs today", "Kazi za leo"),
    "done": ("Done", "Imekamilika"),
    "not_done": ("Not done", "Haijafanyika"),
    "open": ("To do", "Bado"),
    "mark_done": ("Done ✓", "Imekamilika ✓"),
    "couldnt": ("Couldn't do it", "Sikuweza"),
    "reason": ("Why?", "Kwa nini?"),
    "weight_kg": ("Weight (kg)", "Uzito (kg)"),
    "photo": ("Photo", "Picha"),
    "take_photo": ("Take a photo", "Piga picha"),
    "note": ("Note", "Maelezo"),
    "save": ("Save", "Hifadhi"),
    "add_job": ("Add another job", "Ongeza kazi nyingine"),
    "what_job": ("What did you do?", "Ulifanya kazi gani?"),
    "area": ("Area", "Eneo"),
    "block": ("Block", "Shamba"),
    "who": ("Who", "Nani"),
    "today": ("Today", "Leo"),
    "date": ("Date", "Tarehe"),
    "people": ("people", "watu"),
    "overdue": ("Overdue", "Imechelewa"),
    "days": ("days", "siku"),
    "day_n": ("Day", "Siku ya"),
    "of": ("of", "kati ya"),
    "jobs_done": ("jobs done", "kazi zimekamilika"),
    "at_work": ("at work", "kazini"),
    "need_you": ("need you", "zinakuhitaji"),
    "no_jobs": ("No jobs for today yet.", "Hakuna kazi za leo bado."),
    "godfrey_confirms": ("Your day is confirmed by the supervisor in the evening.",
                         "Msimamizi atathibitisha kazi zako jioni."),
    "confirm_day": ("Confirm the day", "Thibitisha siku"),
    "confirmed": ("Confirmed", "Imethibitishwa"),
    "confirmed_by": ("Confirmed by", "Imethibitishwa na"),
    "not_confirmed": ("Not confirmed yet", "Bado haijathibitishwa"),
    "spray": ("Record a spray", "Rekodi unyunyiziaji dawa"),
    "spray_short": ("Spraying", "Dawa"),
    "spray_sub": ("Every spray, every time", "Kila unyunyiziaji, kila mara"),
    "product": ("Product", "Dawa"),
    "other_product": ("Other product (not in the store)", "Dawa nyingine (haiko stoo)"),
    "product_name": ("Product name", "Jina la dawa"),
    "amount": ("Amount of product", "Kiasi cha dawa"),
    "water_l": ("Water (litres)", "Maji (lita)"),
    "pest": ("Pest or disease", "Mdudu au ugonjwa"),
    "sprayed_by": ("Who sprayed", "Aliyenyunyizia"),
    "ppe": ("Gloves and mask worn", "Amevaa glavu na barakoa"),
    "label_photo": ("Photo of the label", "Picha ya lebo"),
    "wait_days": ("Days before harvest (from the label)", "Siku kabla ya kuvuna (kwenye lebo)"),
    "harmful_bees": ("Harmful to bees", "Hatari kwa nyuki"),
    "bee_ack": ("Sprayed in the evening and the apiary keeper was told",
                "Imenyunyiziwa jioni na mtunza nyuki ameambiwa"),
    "safe_from": ("Safe to harvest from", "Salama kuvuna kuanzia"),
    "recent_sprays": ("Recent sprays", "Dawa za karibuni"),
    "report": ("Today's report", "Ripoti ya leo"),
    "report_of": ("Report", "Ripoti"),
    "who_did_what": ("Who did what", "Nani alifanya nini"),
    "weighed": ("Harvests weighed", "Mavuno yaliyopimwa"),
    "needs_you": ("Needs you", "Inakuhitaji"),
    "nothing_needs": ("Nothing needs you today.", "Hakuna kinachokuhitaji leo."),
    "routines": ("Set the routine jobs", "Panga kazi za kawaida"),
    "full_records": ("Full records", "Kumbukumbu zote"),
    "logout": ("Log out", "Toka"),
    "time": ("Time", "Saa"),
    "all": ("All", "Wote"),
    "pick_person": ("Choose the person", "Chagua mtu"),
    "settings": ("Settings", "Mipangilio"),
}


def lang():
    lg = session.get("lang")
    if lg in LANGS:
        return lg
    u = g.get("user")
    return "sw" if u and u["role"] == "worker" else "en"


def t(key):
    pair = WORDS.get(key)
    if not pair:
        return key
    return pair[1] if lang() == "sw" else pair[0]


def area_name(key):
    a = AREA.get(key) or AREA["general"]
    return a["sw"] if lang() == "sw" else a["en"]


def title(row):
    """A job's title in the chosen language (Kiswahili falls back to English)."""
    if lang() == "sw" and row["title_sw"]:
        return row["title_sw"]
    return row["title_en"]


def long_date(d):
    """date -> 'Saturday 31 October' / 'Jumamosi 31 Oktoba'."""
    lg = lang()
    return f"{WEEKDAYS[lg][d.weekday()]} {d.day} {MONTHS[lg][d.month - 1]}"
