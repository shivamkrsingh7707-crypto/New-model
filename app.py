import os
import json
from flask import Flask, render_template_string, request, session, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime, date, timedelta
from urllib.parse import quote as url_quote
from sqlalchemy import func
import math

app = Flask(__name__)

# ═════════════════════════════════════════════
# SECRET KEY — Persistent across restarts
# ═════════════════════════════════════════════
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    _key_file = '.secret_key'
    if os.path.exists(_key_file):
        with open(_key_file, 'r') as f:
            SECRET_KEY = f.read().strip()
    else:
        SECRET_KEY = os.urandom(32).hex()
        try:
            with open(_key_file, 'w') as f:
                f.write(SECRET_KEY)
        except Exception:
            pass
app.secret_key = SECRET_KEY

# ═════════════════════════════════════════════
# DATABASE — PostgreSQL on Render, SQLite locally
# ═════════════════════════════════════════════
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///study_tracker.db')

# SQLAlchemy 2.x compatibility
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

IS_POSTGRES = DATABASE_URL.startswith('postgresql://')

# Connection pool settings for Postgres (critical for Render)
if IS_POSTGRES:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 280,
        'pool_size': 5,
        'max_overflow': 10,
        'pool_timeout': 30,
    }

print(f"🗄️  Database: {'PostgreSQL (Production)' if IS_POSTGRES else 'SQLite (Local)'}")

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ═════════════════════════════════════════════
# LIVE ONLINE TRACKING
# ═════════════════════════════════════════════

active_sessions = {}


def get_live_online_users():
    seen = {}
    for sid, info in active_sessions.items():
        uid = info.get('user_id')
        if uid and uid not in seen:
            seen[uid] = info
    return list(seen.values())


def get_live_online_count():
    return len(get_live_online_users())


def get_room_online_count(room):
    users_in_room = set()
    for sid, info in active_sessions.items():
        if info.get('room') == room and info.get('user_id'):
            users_in_room.add(info['user_id'])
    return len(users_in_room)


# ═════════════════════════════════════════════
# DATABASE MODELS
# ═════════════════════════════════════════════

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    student_class = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_active = db.Column(db.Date, default=date.today)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    streak = db.Column(db.Integer, default=0)
    avatar_color = db.Column(db.String(20), default='#00E1FD')


class ChapterProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    subject = db.Column(db.String(100), nullable=False)
    chapter_index = db.Column(db.Integer, nullable=False)
    circle_index = db.Column(db.Integer, nullable=False)
    completed = db.Column(db.Boolean, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('user_id', 'subject', 'chapter_index', 'circle_index'),
    )


class CustomSubject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    icon = db.Column(db.String(10), default='📘')
    color_from = db.Column(db.String(20), default='#00E1FD')
    color_to = db.Column(db.String(20), default='#C533FF')
    tag = db.Column(db.String(50), default='CUSTOM')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CustomChapter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.Integer, db.ForeignKey('custom_subject.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    order_index = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    activity_date = db.Column(db.Date, default=date.today, index=True)
    circles_completed = db.Column(db.Integer, default=0)
    __table_args__ = (
        db.UniqueConstraint('user_id', 'activity_date'),
    )


class ExamSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    subject = db.Column(db.String(100), nullable=False)
    exam_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('user_id', 'subject'),
    )


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user_name = db.Column(db.String(100), nullable=False)
    user_class = db.Column(db.String(50), nullable=False)
    user_avatar_color = db.Column(db.String(20), default='#00E1FD')
    room = db.Column(db.String(50), default='general', index=True)
    message = db.Column(db.String(1000), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    reply_to_id = db.Column(db.Integer, db.ForeignKey('chat_message.id'), nullable=True)


# ═════════════════════════════════════════════
# CURRICULUM & META
# ═════════════════════════════════════════════

DEFAULT_CURRICULUM = {
    "Science": [
        "Chemical Reactions and Equations", "Acids, Bases and Salts", "Metals and Non-metals",
        "Carbon and its Compounds", "Life Processes", "Control and Coordination",
        "How do Organisms Reproduce?", "Heredity", "Light - Reflection and Refraction",
        "Human Eye and Colourful World", "Electricity", "Magnetic Effects of Electric Current",
        "Our Environment"
    ],
    "Social Science": [
        "Rise of Nationalism in Europe", "Nationalism in India", "The Making of a Global World",
        "Age of Industrialization", "Print Culture & Modern World", "Resources and Development",
        "Forest and Wildlife Resources", "Water Resources", "Agriculture",
        "Minerals and Energy Resources", "Manufacturing Industries", "Lifelines of National Economy",
        "Power Sharing", "Federalism", "Gender, Religion and Caste", "Political Parties",
        "Outcomes of Democracy", "Development", "Sectors of Indian Economy",
        "Money and Credit", "Globalisation & Indian Economy"
    ],
    "Maths": [
        "Real Numbers", "Polynomials", "Pair of Linear Equations", "Quadratic Equations",
        "Arithmetic Progressions", "Triangles", "Coordinate Geometry",
        "Introduction to Trigonometry", "Applications of Trigonometry", "Circles",
        "Areas Related to Circles", "Surface Areas and Volumes", "Statistics", "Probability"
    ],
    "Hindi & English": [
        "A Letter to God (English)", "Nelson Mandela (English)", "Two Stories About Flying (English)",
        "Diary of Anne Frank (English)", "Glimpses of India (English)", "Mijbil the Otter (English)",
        "Madam Rides the Bus (English)", "The Sermon at Benares (English)", "The Proposal (English)",
        "Surdas Ke Pad (Hindi)", "Ram-Lakshman-Parshuram Samvad (Hindi)",
        "Netaji Ka Chashma (Hindi)", "Balgovin Bhagat (Hindi)"
    ]
}

DEFAULT_SUBJECT_META = {
    "Science":         {"icon": "⚗️", "color_from": "#00E1FD", "color_to": "#0077FF", "tag": "PHY · CHM · BIO"},
    "Social Science":  {"icon": "🌍", "color_from": "#C533FF", "color_to": "#7B2FFF", "tag": "HIS · GEO · POL · ECO"},
    "Maths":           {"icon": "📐", "color_from": "#00F260", "color_to": "#0575E6", "tag": "ALG · GEO · TRIG · STAT"},
    "Hindi & English": {"icon": "📚", "color_from": "#FF6B6B", "color_to": "#C533FF", "tag": "LIT · PROSE · POETRY"}
}

COLOR_PRESETS = [
    {"from": "#00E1FD", "to": "#C533FF", "name": "Cyan → Purple"},
    {"from": "#00F260", "to": "#0575E6", "name": "Mint → Blue"},
    {"from": "#FF6B6B", "to": "#C533FF", "name": "Coral → Purple"},
    {"from": "#FFD700", "to": "#FF6B00", "name": "Gold → Orange"},
    {"from": "#00E1FD", "to": "#00F260", "name": "Cyan → Mint"},
    {"from": "#FF00E1", "to": "#7B2FFF", "name": "Pink → Purple"},
]

ICON_PRESETS = ['📘', '📗', '📕', '📙', '📓', '🧪', '🔬', '🧬', '💻', '🎨', '🎵', '⚽', '🌐', '📖', '✏️', '🎯']

AVATAR_COLORS = ['#00E1FD', '#C533FF', '#00F260', '#FFD700', '#FF6B6B', '#FF00E1', '#7B2FFF', '#0575E6']

CIRCLES_PER_CHAPTER = 5
CIRCLE_LABELS = ["Read", "Note", "Prac", "Rev", "Done"]

CHAT_ROOMS = [
    {"id": "general", "name": "General", "icon": "💬", "desc": "Chat about anything"},
    {"id": "science", "name": "Science", "icon": "⚗️", "desc": "Physics, Chem, Bio"},
    {"id": "maths", "name": "Maths", "icon": "📐", "desc": "Algebra, geometry, trig"},
    {"id": "social", "name": "Social", "icon": "🌍", "desc": "History, geo, civics"},
    {"id": "english", "name": "Lang", "icon": "📚", "desc": "Hindi & English lit"},
    {"id": "doubts", "name": "Doubts", "icon": "❓", "desc": "Ask & help others"},
]


# ═════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════

def get_current_user():
    if 'user_id' not in session:
        return None
    return User.query.get(session['user_id'])


def touch_user(user):
    if user:
        try:
            user.last_seen = datetime.utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()


def get_total_user_count():
    return User.query.count()


def time_ago(dt):
    if not dt:
        return "just now"
    now = datetime.utcnow()
    diff = now - dt
    secs = diff.total_seconds()
    if secs < 60:
        return "just now"
    mins = int(secs / 60)
    if mins < 60:
        return f"{mins}m ago"
    hrs = int(mins / 60)
    if hrs < 24:
        return f"{hrs}h ago"
    days = int(hrs / 24)
    if days < 7:
        return f"{days}d ago"
    return dt.strftime('%d %b')


def get_full_curriculum(user_id):
    curriculum = {}
    for name, chapters in DEFAULT_CURRICULUM.items():
        curriculum[name] = {
            "chapters": chapters,
            "meta": DEFAULT_SUBJECT_META[name],
            "is_custom": False,
            "subject_id": None
        }
    customs = CustomSubject.query.filter_by(user_id=user_id).order_by(CustomSubject.created_at).all()
    for cs in customs:
        cchapters = CustomChapter.query.filter_by(subject_id=cs.id).order_by(CustomChapter.order_index, CustomChapter.id).all()
        curriculum[cs.name] = {
            "chapters": [c.name for c in cchapters],
            "meta": {"icon": cs.icon, "color_from": cs.color_from, "color_to": cs.color_to, "tag": cs.tag},
            "is_custom": True,
            "subject_id": cs.id
        }
    return curriculum


def get_user_progress(user_id):
    rows = ChapterProgress.query.filter_by(user_id=user_id, completed=True).all()
    return {f"{r.subject}__{r.chapter_index}__{r.circle_index}": True for r in rows}


def compute_stats(user_id):
    curriculum = get_full_curriculum(user_id)
    total_circles = sum(len(v['chapters']) * CIRCLES_PER_CHAPTER for v in curriculum.values())
    completed_rows = ChapterProgress.query.filter_by(user_id=user_id, completed=True).count()
    overall_pct = round((completed_rows / total_circles) * 100, 1) if total_circles else 0

    active_chambers = 0
    subject_pcts = {}
    subject_done_circles = {}
    for subject, info in curriculum.items():
        subj_total = len(info['chapters']) * CIRCLES_PER_CHAPTER
        subj_done = ChapterProgress.query.filter_by(
            user_id=user_id, subject=subject, completed=True).count()
        pct = round((subj_done / subj_total) * 100, 1) if subj_total else 0
        subject_pcts[subject] = pct
        subject_done_circles[subject] = subj_done
        if subj_done > 0:
            active_chambers += 1

    fully_done_chapters = 0
    total_chapters = sum(len(v['chapters']) for v in curriculum.values())
    for subject, info in curriculum.items():
        for ci in range(len(info['chapters'])):
            done = ChapterProgress.query.filter_by(
                user_id=user_id, subject=subject, chapter_index=ci, completed=True
            ).count()
            if done == CIRCLES_PER_CHAPTER:
                fully_done_chapters += 1
    syllabus_pct = round((fully_done_chapters / total_chapters) * 100, 1) if total_chapters else 0

    return {
        "overall_pct": overall_pct,
        "active_chambers": active_chambers,
        "subject_pcts": subject_pcts,
        "subject_done_circles": subject_done_circles,
        "syllabus_pct": syllabus_pct,
        "completed_circles": completed_rows,
        "total_circles": total_circles,
        "fully_done_chapters": fully_done_chapters,
        "total_chapters": total_chapters,
        "total_subjects": len(curriculum)
    }


def update_streak(user):
    today = date.today()
    if user.last_active == today:
        return
    if user.last_active == today - timedelta(days=1):
        user.streak = (user.streak or 0) + 1
    else:
        user.streak = 1
    user.last_active = today
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def log_activity(user_id, delta):
    today = date.today()
    log = ActivityLog.query.filter_by(user_id=user_id, activity_date=today).first()
    if log:
        log.circles_completed = max(0, (log.circles_completed or 0) + delta)
    else:
        if delta > 0:
            log = ActivityLog(user_id=user_id, activity_date=today, circles_completed=delta)
            db.session.add(log)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def get_7day_data(user_id):
    today = date.today()
    days = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        log = ActivityLog.query.filter_by(user_id=user_id, activity_date=d).first()
        count = log.circles_completed if log else 0
        days.append({
            "date": d.strftime('%d %b'),
            "day_label": d.strftime('%a'),
            "count": count,
            "is_today": (d == today)
        })
    return days


def compute_exam_plans(user_id):
    curriculum = get_full_curriculum(user_id)
    stats = compute_stats(user_id)
    exams = ExamSchedule.query.filter_by(user_id=user_id).all()
    today = date.today()
    plans = []
    for exam in exams:
        if exam.subject not in curriculum:
            continue
        info = curriculum[exam.subject]
        total_ch = len(info['chapters'])
        total_units = total_ch * CIRCLES_PER_CHAPTER
        done_units = stats['subject_done_circles'].get(exam.subject, 0)
        remaining_units = max(0, total_units - done_units)

        remaining_chapters = 0
        for ci in range(total_ch):
            done = ChapterProgress.query.filter_by(
                user_id=user_id, subject=exam.subject, chapter_index=ci, completed=True
            ).count()
            if done < CIRCLES_PER_CHAPTER:
                remaining_chapters += 1

        days_left = (exam.exam_date - today).days

        if days_left < 0:
            status = "past"
            per_day_units = 0; per_day_chapters = 0; per_week_chapters = 0
        elif days_left == 0:
            status = "today"
            per_day_units = remaining_units
            per_day_chapters = remaining_chapters
            per_week_chapters = remaining_chapters
        else:
            status = "upcoming"
            per_day_units = math.ceil(remaining_units / days_left) if remaining_units > 0 else 0
            per_day_chapters = round(remaining_chapters / days_left, 2) if remaining_chapters > 0 else 0
            per_week_chapters = math.ceil(remaining_chapters / max(days_left / 7, 1)) if remaining_chapters > 0 else 0

        pct = stats['subject_pcts'].get(exam.subject, 0)
        if remaining_units == 0:
            feasibility = "complete"
        elif days_left < 0:
            feasibility = "expired"
        elif per_day_units <= 3:
            feasibility = "easy"
        elif per_day_units <= 7:
            feasibility = "moderate"
        elif per_day_units <= 15:
            feasibility = "tough"
        else:
            feasibility = "urgent"

        plans.append({
            "id": exam.id, "subject": exam.subject,
            "exam_date": exam.exam_date,
            "exam_date_str": exam.exam_date.strftime('%d %b %Y'),
            "exam_day": exam.exam_date.strftime('%A'),
            "days_left": days_left,
            "total_chapters": total_ch,
            "remaining_chapters": remaining_chapters,
            "total_units": total_units,
            "remaining_units": remaining_units,
            "done_units": done_units,
            "per_day_units": per_day_units,
            "per_day_chapters": per_day_chapters,
            "per_week_chapters": per_week_chapters,
            "progress_pct": pct,
            "meta": info['meta'],
            "status": status,
            "feasibility": feasibility
        })
    plans.sort(key=lambda p: p['exam_date'])
    return plans


@app.template_filter('urlencode')
def urlencode_filter(s):
    return url_quote(str(s), safe='')


@app.template_filter('initials')
def initials_filter(name):
    if not name:
        return "?"
    parts = name.strip().split()
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


# ═════════════════════════════════════════════
# BASE STYLE
# ═════════════════════════════════════════════

BASE_STYLE = """
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0"/>
<meta name="theme-color" content="#0B0F19"/>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
  tailwind.config = {
    theme: { extend: {
      colors: {
        'app-bg': '#0B0F19', 'card-bg': '#1A2035',
        'cyan-neon': '#00E1FD', 'purple-neon': '#C533FF',
        'green-neon': '#00F260', 'muted': '#8892A4'
      },
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] }
    }}
  }
</script>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet"/>
<style>
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { background:#0B0F19; font-family:'Inter',sans-serif; color:#E2E8F0; min-height:100vh; overflow-x:hidden; }
  a { text-decoration:none; }
  .glass-nav { background:rgba(11,15,25,0.92); backdrop-filter:blur(24px); border-top:1px solid rgba(0,225,253,0.12); }
  .gradient-text { background:linear-gradient(135deg,#00E1FD,#C533FF); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
  .gradient-btn { background:linear-gradient(135deg,#00E1FD,#C533FF); transition:all 0.3s ease; }
  .gradient-btn:hover { opacity:0.92; transform:translateY(-1px); box-shadow:0 8px 25px rgba(0,225,253,0.3); }
  .gradient-btn:disabled { opacity:0.5; cursor:not-allowed; }
  .card-base { background:#1A2035; border:1px solid rgba(255,255,255,0.06); border-radius:16px; transition:all 0.25s ease; }
  .card-glow:hover { box-shadow:0 0 30px rgba(0,225,253,0.08), 0 8px 32px rgba(0,0,0,0.4); transform:translateY(-2px); }
  .circle-btn { transition:all 0.2s cubic-bezier(0.34,1.56,0.64,1); cursor:pointer; user-select:none; }
  .circle-btn:hover { transform:scale(1.12); }
  .circle-btn:active { transform:scale(0.94); }
  .nav-icon { transition:all 0.2s ease; }
  .nav-icon:hover { transform:translateY(-2px); }
  .scroll-content { padding-bottom:100px; }
  ::-webkit-scrollbar { width:4px; }
  ::-webkit-scrollbar-track { background:#0B0F19; }
  ::-webkit-scrollbar-thumb { background:#1A2035; border-radius:4px; }
  .input-field { background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.1); border-radius:12px; color:#E2E8F0; outline:none; transition:all 0.2s ease; }
  .input-field:focus { border-color:#00E1FD; box-shadow:0 0 0 3px rgba(0,225,253,0.12); }
  .chapter-row { background:rgba(255,255,255,0.02); border:1px solid rgba(255,255,255,0.05); border-radius:14px; transition:all 0.2s ease; }
  .chapter-row:hover { background:rgba(255,255,255,0.04); border-color:rgba(0,225,253,0.15); }
  .tag-pill { background:rgba(255,255,255,0.06); border-radius:999px; }
  @keyframes fadeInUp { from{opacity:0;transform:translateY(20px)} to{opacity:1;transform:translateY(0)} }
  .fade-in { animation:fadeInUp 0.5s ease forwards; }
  @keyframes msgIn { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
  .msg-in { animation:msgIn 0.3s ease forwards; }
  @keyframes pulseGlow { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.5;transform:scale(1.2)} }
  .online-pulse { animation:pulseGlow 2s ease infinite; }
  @keyframes typingDot { 0%,60%,100%{transform:translateY(0);opacity:0.4} 30%{transform:translateY(-4px);opacity:1} }
  .typing-dot { display:inline-block; width:6px; height:6px; border-radius:50%; background:#00E1FD; margin:0 2px; animation:typingDot 1.4s infinite; }
  .typing-dot:nth-child(2){animation-delay:0.15s;} .typing-dot:nth-child(3){animation-delay:0.3s;}
  .subject-card { border-radius:20px; overflow:hidden; position:relative; transition:all 0.3s ease; cursor:pointer; }
  .subject-card:hover { transform:translateY(-4px); box-shadow:0 20px 40px rgba(0,0,0,0.5); }
  .modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.7); backdrop-filter:blur(6px); z-index:100; display:none; align-items:center; justify-content:center; padding:16px; }
  .modal-overlay.active { display:flex; animation:fadeInUp 0.3s ease; }
  .modal-content { background:#1A2035; border:1px solid rgba(255,255,255,0.08); border-radius:20px; width:100%; max-width:440px; max-height:90vh; overflow-y:auto; }
  .icon-choice, .color-choice { cursor:pointer; transition:all 0.2s ease; }
  .icon-choice:hover, .color-choice:hover { transform:scale(1.1); }
  .icon-choice.active { background:rgba(0,225,253,0.15) !important; border-color:#00E1FD !important; }
  .color-choice.active { transform:scale(1.15); box-shadow:0 0 0 3px #fff; }
  .bar-chart-bar { transition:all 0.6s cubic-bezier(0.34,1.2,0.64,1); }
  .chapter-item { background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.06); border-radius:10px; padding:10px 12px; display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .btn-secondary { background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:10px; padding:8px 14px; font-size:13px; font-weight:600; color:#E2E8F0; cursor:pointer; transition:all 0.2s; }
  .btn-secondary:hover { background:rgba(255,255,255,0.08); border-color:rgba(0,225,253,0.3); }
  .btn-danger { background:rgba(255,100,100,0.08); border:1px solid rgba(255,100,100,0.3); color:#FF6B6B; }
  .btn-danger:hover { background:rgba(255,100,100,0.15); }
  .exam-card { border-radius:18px; padding:18px; position:relative; overflow:hidden; }
  .countdown-badge { display:inline-flex; align-items:center; gap:4px; padding:4px 10px; border-radius:999px; font-size:11px; font-weight:800; }
  input[type="date"] { color-scheme:dark; }
  .avatar { width:36px; height:36px; border-radius:999px; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:12px; color:#0B0F19; flex-shrink:0; }
  .chat-bubble { border-radius:14px; padding:10px 14px; max-width:78%; word-wrap:break-word; line-height:1.4; font-size:13.5px; }
  .chat-bubble.mine { background:linear-gradient(135deg,#00E1FD,#C533FF); color:#fff; border-bottom-right-radius:4px; }
  .chat-bubble.theirs { background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.08); color:#E2E8F0; border-bottom-left-radius:4px; }
  .chat-scroll { height:calc(100vh - 340px); min-height:300px; overflow-y:auto; padding:8px 12px; scroll-behavior:smooth; }
  .chat-input-bar { position:fixed; bottom:78px; left:0; right:0; background:rgba(11,15,25,0.96); backdrop-filter:blur(20px); border-top:1px solid rgba(255,255,255,0.06); z-index:40; padding:10px 16px; }
  .reply-preview { background:rgba(0,225,253,0.06); border-left:3px solid #00E1FD; border-radius:8px; padding:6px 10px; margin-bottom:8px; font-size:12px; display:flex; align-items:center; justify-content:space-between; }
  .room-pill { transition:all 0.2s ease; }
  .room-pill.active { background:linear-gradient(135deg,rgba(0,225,253,0.18),rgba(197,51,255,0.18)); border-color:#00E1FD !important; color:#fff !important; }
  .status-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .status-dot.connected { background:#00F260; box-shadow:0 0 8px rgba(0,242,96,0.5); }
  .status-dot.connecting { background:#FFD700; }
  .status-dot.disconnected { background:#FF6B6B; }
  .join-toast { position:fixed; top:20px; left:50%; transform:translateX(-50%); background:rgba(26,32,53,0.95); border:1px solid rgba(0,225,253,0.3); border-radius:999px; padding:8px 18px; font-size:12px; font-weight:600; color:#E2E8F0; z-index:200; box-shadow:0 8px 24px rgba(0,0,0,0.5); backdrop-filter:blur(20px); }
</style>
"""


# ═════════════════════════════════════════════
# BOTTOM NAV
# ═════════════════════════════════════════════

def bottom_nav_html(active):
    icons = {
        "hub": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>',
        "exam": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
        "chat": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
        "analytics": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
        "settings": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    }
    def item(key, label, href):
        is_active = (key == active)
        wrap_style = ("background:linear-gradient(135deg,rgba(0,225,253,0.15),rgba(197,51,255,0.15));"
                      "border-color:rgba(0,225,253,0.35);") if is_active else \
                     ("background:rgba(255,255,255,0.04);border-color:rgba(255,255,255,0.08);")
        stroke_color = "#00E1FD" if is_active else "#8892A4"
        text_class = 'text-[9px] font-semibold' if is_active else 'text-[9px] font-medium text-muted'
        text_style = 'color:#00E1FD;' if is_active else ''
        return f'''
        <a href="{href}" class="nav-icon flex flex-col items-center gap-0.5 flex-1">
          <div class="icon-wrap w-9 h-9 rounded-2xl flex items-center justify-center border border-transparent" style="{wrap_style}">
            <span style="color:{stroke_color};display:flex;">{icons[key].replace('stroke-width', f'stroke="{stroke_color}" stroke-width')}</span>
          </div>
          <span class="{text_class}" style="{text_style}">{label}</span>
        </a>'''
    return f'''
    <nav class="fixed bottom-0 left-0 right-0 glass-nav z-50">
      <div class="max-w-2xl mx-auto px-3 py-2.5 flex items-center justify-around gap-1">
        {item("hub", "Hub", "/")}
        {item("exam", "Exam", "/exam-zone")}
        {item("chat", "Chat", "/chat")}
        {item("analytics", "Stats", "/analytics")}
        {item("settings", "Setup", "/settings")}
      </div>
    </nav>'''


def presence_script():
    return """
    <script>
      (function() {
        if (typeof io === 'undefined') return;
        try {
          const _presenceSock = io({transports: ['websocket','polling']});
          _presenceSock.on('presence_update', (data) => {
            document.querySelectorAll('.js-online-count').forEach(el => {
              el.textContent = data.online;
            });
          });
        } catch(e) { console.warn('Presence socket failed:', e); }
      })();
    </script>
    """


# ═════════════════════════════════════════════
# TEMPLATES
# ═════════════════════════════════════════════

ONBOARDING_TEMPLATE = """
<!DOCTYPE html><html lang="en"><head>
<title>Study Tracker — Setup</title>
""" + BASE_STYLE + """
</head><body class="flex items-center justify-center min-h-screen p-4">
  <div class="w-full max-w-md fade-in">
    <div class="absolute top-0 left-1/2 -translate-x-1/2 w-96 h-96 rounded-full opacity-10 pointer-events-none" style="background:radial-gradient(circle,#00E1FD 0%,transparent 70%);filter:blur(60px);"></div>
    <div class="card-base p-8 relative overflow-hidden" style="box-shadow:0 25px 60px rgba(0,0,0,0.5);">
      <div class="absolute -top-10 -right-10 w-40 h-40 rounded-full opacity-20" style="background:linear-gradient(135deg,#00E1FD,#C533FF);filter:blur(40px);"></div>
      <div class="absolute -bottom-10 -left-10 w-32 h-32 rounded-full opacity-15" style="background:linear-gradient(135deg,#C533FF,#00E1FD);filter:blur(40px);"></div>
      <div class="relative z-10">
        <div class="flex items-center gap-3 mb-8">
          <div class="w-12 h-12 rounded-2xl flex items-center justify-center text-2xl" style="background:linear-gradient(135deg,#00E1FD22,#C533FF22);border:1px solid rgba(0,225,253,0.3);">🎯</div>
          <div>
            <div class="gradient-text font-black text-xl tracking-tight">StudyTracker</div>
            <div class="text-xs text-muted">Class 10 Academic Hub</div>
          </div>
        </div>
        <h1 class="text-3xl font-black text-white mb-2 leading-tight">Set Up Your<br/><span class="gradient-text">Command Center</span></h1>
        <p class="text-muted text-sm mb-8">Join {{ total_users }} student{{ 's' if total_users != 1 else '' }} on their learning journey.</p>
        {% if error %}
        <div class="mb-4 p-3 rounded-xl text-sm" style="background:rgba(255,100,100,0.1);border:1px solid rgba(255,100,100,0.3);color:#FF6B6B;">⚠️ {{ error }}</div>
        {% endif %}
        <form method="POST" action="/setup" class="space-y-4">
          <div>
            <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Your Name</label>
            <input type="text" name="name" placeholder="e.g. Arjun Sharma" required maxlength="100" class="input-field w-full px-4 py-3.5 text-sm font-medium placeholder-gray-600"/>
          </div>
          <div>
            <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Your Class</label>
            <input type="text" name="student_class" placeholder="e.g. Class 10 — A" required maxlength="50" class="input-field w-full px-4 py-3.5 text-sm font-medium placeholder-gray-600"/>
          </div>
          <button type="submit" class="gradient-btn w-full py-4 rounded-xl text-white font-bold text-sm tracking-wide mt-2">🚀 Launch My Dashboard</button>
        </form>
        <div class="flex items-center justify-center gap-2 mt-6 text-xs text-muted">
          <span class="inline-block w-2 h-2 rounded-full online-pulse" style="background:#00F260;"></span>
          <span class="js-online-count">{{ online_count }}</span>
          <span>student{{ 's' if online_count != 1 else '' }} online right now</span>
        </div>
      </div>
    </div>
  </div>
""" + presence_script() + """
</body></html>
"""


DASHBOARD_TEMPLATE = """
<!DOCTYPE html><html lang="en"><head>
<title>StudyTracker — Dashboard</title>
""" + BASE_STYLE + """
</head><body>
  <div class="fixed pointer-events-none" style="top:-100px;left:-100px;width:400px;height:400px;background:radial-gradient(circle,rgba(0,225,253,0.06) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>
  <div class="fixed pointer-events-none" style="bottom:-100px;right:-100px;width:500px;height:500px;background:radial-gradient(circle,rgba(197,51,255,0.06) 0%,transparent 70%);filter:blur(80px);z-index:0;"></div>

  <div class="relative z-10 scroll-content">
    <div class="max-w-2xl mx-auto px-4 pt-6 pb-4">

      <div class="flex items-center justify-between mb-4 fade-in">
        <a href="/chat" class="flex items-center gap-2 px-3 py-1.5 rounded-full" style="background:rgba(0,242,96,0.08);border:1px solid rgba(0,242,96,0.25);">
          <span class="inline-block w-2 h-2 rounded-full online-pulse" style="background:#00F260;"></span>
          <span class="text-xs font-bold js-online-count" style="color:#00F260;">{{ online_count }}</span>
          <span class="text-xs text-muted">live now</span>
        </a>
        <div class="text-xs text-muted">{{ total_users }} total learner{{ 's' if total_users != 1 else '' }}</div>
      </div>

      <div class="flex items-center justify-between mb-6 fade-in">
        <div>
          <p class="text-xs font-semibold text-muted uppercase tracking-widest mb-1">Welcome back 👋</p>
          <h1 class="text-2xl font-black text-white leading-tight">{{ user.name }}</h1>
          <div class="flex items-center gap-2 mt-1">
            <span class="text-xs px-2 py-0.5 rounded-full font-semibold" style="background:rgba(0,225,253,0.12);color:#00E1FD;border:1px solid rgba(0,225,253,0.2);">{{ user.student_class }}</span>
            <span class="text-xs text-muted">· Academic Hub</span>
          </div>
        </div>
        <div class="avatar" style="background:linear-gradient(135deg,{{ user.avatar_color }},#C533FF);width:44px;height:44px;font-size:14px;">{{ user.name | initials }}</div>
      </div>

      {% if nearest_exam %}
      <a href="/exam-zone" class="card-base card-glow p-4 mb-4 fade-in block" style="animation-delay:0.05s;background:linear-gradient(135deg,rgba(255,107,107,0.08),rgba(197,51,255,0.08));border-color:rgba(255,107,107,0.2);">
        <div class="flex items-center gap-3">
          <div class="text-3xl">📅</div>
          <div class="flex-1">
            <div class="text-xs text-muted font-semibold uppercase tracking-wider">Next Exam</div>
            <div class="text-sm font-bold text-white">{{ nearest_exam.subject }} · <span style="color:#FF6B6B;">{{ nearest_exam.days_left }} day{{ 's' if nearest_exam.days_left != 1 else '' }} left</span></div>
            <div class="text-xs text-muted">Study <span class="font-bold" style="color:#00E1FD;">{{ nearest_exam.per_day_units }} units/day</span> to finish in time →</div>
          </div>
        </div>
      </a>
      {% endif %}

      <div class="card-base card-glow p-5 mb-4 fade-in" style="animation-delay:0.1s;">
        <div class="flex items-center gap-5">
          <div class="relative flex-shrink-0" style="width:100px;height:100px;">
            <svg width="100" height="100" viewBox="0 0 100 100" style="transform:rotate(-90deg);">
              <circle cx="50" cy="50" r="40" fill="none" stroke="rgba(255,255,255,0.07)" stroke-width="10"/>
              <circle cx="50" cy="50" r="40" fill="none" stroke="url(#ringGrad)" stroke-width="10" stroke-linecap="round"
                stroke-dasharray="{{ ring_dash }} {{ ring_gap }}" style="transition:stroke-dasharray 0.8s ease;"/>
              <defs><linearGradient id="ringGrad" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" style="stop-color:#00E1FD"/><stop offset="100%" style="stop-color:#C533FF"/></linearGradient></defs>
            </svg>
            <div class="absolute inset-0 flex flex-col items-center justify-center">
              <span class="text-xl font-black text-white leading-none">{{ stats.overall_pct }}%</span>
              <span class="text-xs text-muted mt-0.5">Sync</span>
            </div>
          </div>
          <div class="flex-1">
            <h2 class="text-lg font-bold text-white mb-0.5">Overall Progress</h2>
            <p class="text-xs text-muted mb-3">{{ stats.completed_circles }}/{{ stats.total_circles }} learning units</p>
            <div class="w-full rounded-full h-2" style="background:rgba(255,255,255,0.07);">
              <div class="h-2 rounded-full" style="width:{{ stats.overall_pct }}%;background:linear-gradient(90deg,#00E1FD,#C533FF);transition:width 0.8s ease;"></div>
            </div>
          </div>
        </div>
      </div>

      <div class="card-base p-5 mb-4 fade-in" style="animation-delay:0.13s;">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-sm font-bold text-white">Past 7 Days</h2>
            <p class="text-xs text-muted">Daily learning activity</p>
          </div>
          <div class="text-right">
            <div class="text-lg font-black gradient-text">{{ week_total }}</div>
            <div class="text-xs text-muted">units this week</div>
          </div>
        </div>
        <div class="flex items-end justify-between gap-2" style="height:110px;">
          {% for day in week_data %}
          {% set bar_height = (day.count / week_max * 100) if week_max > 0 else 0 %}
          <div class="flex flex-col items-center flex-1 gap-1.5">
            <div class="w-full flex flex-col justify-end items-center" style="height:80px;">
              {% if day.count > 0 %}
              <span class="text-xs font-bold mb-1" style="color:{% if day.is_today %}#00E1FD{% else %}#E2E8F0{% endif %};font-size:10px;">{{ day.count }}</span>
              {% endif %}
              <div class="bar-chart-bar w-full rounded-t-lg" style="
                height:{{ bar_height if bar_height >= 6 else (6 if day.count > 0 else 3) }}%;
                background:{% if day.is_today %}linear-gradient(180deg,#00E1FD,#C533FF){% elif day.count > 0 %}linear-gradient(180deg,rgba(0,225,253,0.6),rgba(197,51,255,0.4)){% else %}rgba(255,255,255,0.06){% endif %};
                min-height:4px;
                box-shadow:{% if day.is_today %}0 0 12px rgba(0,225,253,0.4){% else %}none{% endif %};
              "></div>
            </div>
            <div class="text-center">
              <div class="text-xs font-semibold" style="color:{% if day.is_today %}#00E1FD{% else %}#8892A4{% endif %};font-size:10px;">{{ day.day_label }}</div>
              <div class="text-muted" style="font-size:9px;">{{ day.date }}</div>
            </div>
          </div>
          {% endfor %}
        </div>
      </div>

      <div class="grid grid-cols-3 gap-3 mb-4 fade-in" style="animation-delay:0.15s;">
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1">🔥</div>
          <div class="text-xl font-black text-white">{{ user.streak or 0 }}</div>
          <div class="text-xs text-muted leading-tight mt-0.5">Day<br/>Streak</div>
        </div>
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1">⚡</div>
          <div class="text-xl font-black gradient-text">{{ stats.active_chambers }}/{{ stats.total_subjects }}</div>
          <div class="text-xs text-muted leading-tight mt-0.5">Active<br/>Chambers</div>
        </div>
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1">📊</div>
          <div class="text-xl font-black" style="color:#00F260;">{{ stats.syllabus_pct }}%</div>
          <div class="text-xs text-muted leading-tight mt-0.5">Chapters<br/>Done</div>
        </div>
      </div>

      <div class="mb-2 fade-in" style="animation-delay:0.2s;">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-base font-bold text-white">Subject Chambers</h2>
          <button onclick="openAddSubject()" class="text-xs font-bold gradient-text flex items-center gap-1">+ Add Subject</button>
        </div>
        <div class="grid grid-cols-2 gap-3">
          {% for subject, info in curriculum.items() %}
          {% set meta = info.meta %}
          <a href="/subject/{{ subject | urlencode }}" class="subject-card card-base block" style="border:1px solid rgba(255,255,255,0.06);">
            <div class="p-5">
              <div class="flex items-start justify-between mb-4">
                <div class="w-11 h-11 rounded-xl flex items-center justify-center text-xl" style="background:linear-gradient(135deg,{{ meta.color_from }}22,{{ meta.color_to }}22);border:1px solid {{ meta.color_from }}33;">{{ meta.icon }}</div>
                <div class="text-right">
                  <div class="text-lg font-black text-white">{{ stats.subject_pcts[subject] }}%</div>
                  <div class="text-xs text-muted">Done</div>
                </div>
              </div>
              <div class="flex items-center gap-1.5">
                <h3 class="text-sm font-bold text-white leading-tight">{{ subject }}</h3>
                {% if info.is_custom %}<span class="text-xs" style="color:#00F260;">●</span>{% endif %}
              </div>
              <span class="tag-pill text-xs px-2 py-0.5 font-medium mt-1 inline-block" style="color:{{ meta.color_from }};font-size:10px;">{{ meta.tag }}</span>
              <div class="mt-3 w-full rounded-full h-1.5" style="background:rgba(255,255,255,0.07);">
                <div class="h-1.5 rounded-full" style="width:{{ stats.subject_pcts[subject] }}%;background:linear-gradient(90deg,{{ meta.color_from }},{{ meta.color_to }});transition:width 0.8s ease;"></div>
              </div>
              <div class="flex justify-between mt-1.5">
                <span class="text-muted" style="font-size:10px;">{{ info.chapters | length }} chapter{{ 's' if info.chapters|length != 1 else '' }}</span>
                <span style="font-size:10px;color:{{ meta.color_from }};">→</span>
              </div>
            </div>
          </a>
          {% endfor %}
          <button onclick="openAddSubject()" class="subject-card card-base block text-left" style="border:2px dashed rgba(0,225,253,0.25);background:rgba(0,225,253,0.02);">
            <div class="p-5 flex flex-col items-center justify-center h-full min-h-[160px]">
              <div class="w-11 h-11 rounded-xl flex items-center justify-center text-2xl mb-3" style="background:linear-gradient(135deg,#00E1FD22,#C533FF22);border:1px solid rgba(0,225,253,0.3);">+</div>
              <h3 class="text-sm font-bold text-white text-center">Add Subject</h3>
              <p class="text-xs text-muted mt-1 text-center">Create custom chamber</p>
            </div>
          </button>
        </div>
      </div>

      <a href="/chat" class="mt-4 fade-in block card-base card-glow" style="animation-delay:0.25s;border-radius:16px;background:linear-gradient(135deg,rgba(0,225,253,0.08),rgba(197,51,255,0.08));border:1px solid rgba(0,225,253,0.15);padding:16px 20px;">
        <div class="flex items-center gap-3">
          <span class="text-2xl">💬</span>
          <div class="flex-1">
            <div class="text-sm font-semibold text-white">Join Live Chat</div>
            <div class="text-xs text-muted"><span class="js-online-count">{{ online_count }}</span> learner{{ 's' if online_count != 1 else '' }} online · Real-time discussion →</div>
          </div>
        </div>
      </a>
    </div>
  </div>

  <div id="addSubjectModal" class="modal-overlay">
    <div class="modal-content p-6 fade-in">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-lg font-black text-white">Create New Subject</h2>
        <button onclick="closeAddSubject()" class="w-8 h-8 rounded-lg flex items-center justify-center" style="background:rgba(255,255,255,0.05);">✕</button>
      </div>
      <form method="POST" action="/add_subject" class="space-y-4">
        <div>
          <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Subject Name</label>
          <input type="text" name="name" placeholder="e.g. Physics Advanced" required maxlength="100" class="input-field w-full px-4 py-3 text-sm font-medium"/>
        </div>
        <div>
          <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Short Tag (optional)</label>
          <input type="text" name="tag" placeholder="e.g. PHY · ADV" maxlength="50" class="input-field w-full px-4 py-3 text-sm font-medium"/>
        </div>
        <div>
          <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Choose Icon</label>
          <div class="grid grid-cols-8 gap-2">
            {% for icon in icon_presets %}
            <div class="icon-choice w-10 h-10 rounded-xl flex items-center justify-center text-lg border{% if loop.first %} active{% endif %}" 
                 style="background:rgba(255,255,255,0.04);border-color:rgba(255,255,255,0.1);"
                 onclick="selectIcon(this, '{{ icon }}')">{{ icon }}</div>
            {% endfor %}
          </div>
          <input type="hidden" name="icon" id="iconInput" value="{{ icon_presets[0] }}"/>
        </div>
        <div>
          <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Color Theme</label>
          <div class="flex gap-3 flex-wrap">
            {% for c in color_presets %}
            <div class="color-choice w-10 h-10 rounded-full{% if loop.first %} active{% endif %}"
                 style="background:linear-gradient(135deg,{{ c.from }},{{ c.to }});"
                 onclick="selectColor(this, '{{ c.from }}', '{{ c.to }}')" title="{{ c.name }}"></div>
            {% endfor %}
          </div>
          <input type="hidden" name="color_from" id="colorFromInput" value="{{ color_presets[0].from }}"/>
          <input type="hidden" name="color_to" id="colorToInput" value="{{ color_presets[0].to }}"/>
        </div>
        <button type="submit" class="gradient-btn w-full py-3 rounded-xl text-white font-bold text-sm mt-2">✨ Create Subject</button>
      </form>
    </div>
  </div>

  <script>
    function openAddSubject() { document.getElementById('addSubjectModal').classList.add('active'); }
    function closeAddSubject() { document.getElementById('addSubjectModal').classList.remove('active'); }
    function selectIcon(el, icon) {
      document.querySelectorAll('.icon-choice').forEach(e => e.classList.remove('active'));
      el.classList.add('active');
      document.getElementById('iconInput').value = icon;
    }
    function selectColor(el, from, to) {
      document.querySelectorAll('.color-choice').forEach(e => e.classList.remove('active'));
      el.classList.add('active');
      document.getElementById('colorFromInput').value = from;
      document.getElementById('colorToInput').value = to;
    }
    document.getElementById('addSubjectModal').addEventListener('click', e => {
      if (e.target.id === 'addSubjectModal') closeAddSubject();
    });
  </script>
  """ + presence_script() + """
  {{ nav_html | safe }}
</body></html>
"""


SUBJECT_TEMPLATE = """
<!DOCTYPE html><html lang="en"><head>
<title>{{ subject }} — StudyTracker</title>
""" + BASE_STYLE + """
<script>
const SUBJECT = {{ subject | tojson }};
const IS_CUSTOM = {{ 'true' if info.is_custom else 'false' }};
const SUBJECT_ID = {{ info.subject_id if info.subject_id else 'null' }};

async function toggleCircle(chapterIdx, circleIdx, el) {
  const filled = el.getAttribute('data-filled') === 'true';
  const newState = !filled;
  setCircleState(el, newState);
  try {
    const resp = await fetch('/update_progress', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({subject: SUBJECT, chapter_index: chapterIdx, circle_index: circleIdx, completed: newState})
    });
    const data = await resp.json();
    if (!data.success) { setCircleState(el, filled); return; }
    updateChapterBar(chapterIdx);
    updateSubjectProgress(data.subject_pct);
  } catch(e) { setCircleState(el, filled); }
}
function setCircleState(el, filled) {
  el.setAttribute('data-filled', filled ? 'true':'false');
  const check = el.querySelector('.circle-check');
  const label = el.querySelector('.circle-label');
  if (filled) {
    el.style.background = 'linear-gradient(135deg,#00E1FD,#C533FF)';
    el.style.borderColor = 'transparent';
    el.style.boxShadow = '0 0 12px rgba(0,225,253,0.5)';
    if (check) check.style.opacity = '1';
    if (label) label.style.opacity = '0';
  } else {
    el.style.background = 'rgba(255,255,255,0.04)';
    el.style.borderColor = 'rgba(255,255,255,0.12)';
    el.style.boxShadow = 'none';
    if (check) check.style.opacity = '0';
    if (label) label.style.opacity = '1';
  }
}
function updateChapterBar(chapterIdx) {
  const row = document.querySelector(`[data-chapter="${chapterIdx}"]`);
  if (!row) return;
  const circles = row.querySelectorAll('.circle-btn');
  let filled = 0;
  circles.forEach(c => { if (c.getAttribute('data-filled') === 'true') filled++; });
  const pct = (filled / circles.length) * 100;
  const bar = row.querySelector('.chapter-bar');
  const pctLabel = row.querySelector('.chapter-pct');
  if (bar) bar.style.width = pct + '%';
  if (pctLabel) pctLabel.textContent = Math.round(pct) + '%';
  if (pct === 100) {
    row.style.borderColor = 'rgba(0,242,96,0.3)';
    row.style.background = 'rgba(0,242,96,0.04)';
    if (pctLabel) { pctLabel.textContent = '✓ Done'; pctLabel.style.color = '#00F260'; }
  } else {
    row.style.borderColor = 'rgba(255,255,255,0.05)';
    row.style.background = 'rgba(255,255,255,0.02)';
    if (pctLabel) pctLabel.style.color = '{{ meta.color_from }}';
  }
}
function updateSubjectProgress(pct) {
  const el = document.getElementById('subject-pct');
  const bar = document.getElementById('subject-bar');
  if (el) el.textContent = pct + '%';
  if (bar) bar.style.width = pct + '%';
}
function openAddChapter() { document.getElementById('addChapterModal').classList.add('active'); }
function closeAddChapter() { document.getElementById('addChapterModal').classList.remove('active'); }
async function deleteChapter(chapterIdx, chapterName) {
  if (!IS_CUSTOM) { alert('Default curriculum chapters cannot be deleted.'); return; }
  if (!confirm(`Delete chapter "${chapterName}"? All progress will be lost.`)) return;
  const resp = await fetch('/delete_chapter', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({subject_id: SUBJECT_ID, chapter_index: chapterIdx})
  });
  const data = await resp.json();
  if (data.success) location.reload();
  else alert('Failed: ' + (data.error || 'Unknown error'));
}
async function deleteSubject() {
  if (!confirm(`Delete the entire subject "${SUBJECT}"? All chapters and progress will be permanently deleted.`)) return;
  const resp = await fetch('/delete_subject', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({subject_id: SUBJECT_ID})
  });
  const data = await resp.json();
  if (data.success) window.location.href = '/';
  else alert('Failed: ' + (data.error || 'Unknown error'));
}
</script>
</head><body>
  <div class="fixed pointer-events-none" style="top:-80px;right:-80px;width:350px;height:350px;background:radial-gradient(circle,{{ meta.color_from }}0D 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>
  <div class="fixed pointer-events-none" style="bottom:-80px;left:-80px;width:350px;height:350px;background:radial-gradient(circle,{{ meta.color_to }}0D 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>

  <div class="relative z-10 scroll-content">
    <div class="max-w-2xl mx-auto px-4 pt-6 pb-4">

      <div class="flex items-center gap-4 mb-6 fade-in">
        <a href="/" class="w-10 h-10 rounded-xl flex items-center justify-center" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#8892A4" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
        </a>
        <div class="flex-1">
          <div class="flex items-center gap-2">
            <span class="text-2xl">{{ meta.icon }}</span>
            <h1 class="text-xl font-black text-white">{{ subject }}</h1>
            {% if info.is_custom %}<span class="text-xs px-2 py-0.5 rounded-full font-bold" style="background:rgba(0,242,96,0.12);color:#00F260;border:1px solid rgba(0,242,96,0.25);">CUSTOM</span>{% endif %}
          </div>
          <div class="flex items-center gap-2 mt-0.5">
            <span class="tag-pill text-xs px-2 py-0.5 font-medium" style="color:{{ meta.color_from }};font-size:10px;">{{ meta.tag }}</span>
            <span class="text-xs text-muted">· {{ chapters | length }} chapter{{ 's' if chapters|length != 1 else '' }}</span>
          </div>
        </div>
      </div>

      <div class="card-base p-4 mb-5 fade-in" style="animation-delay:0.1s;">
        <div class="flex items-center justify-between mb-2">
          <span class="text-sm font-semibold text-white">Subject Sync</span>
          <span id="subject-pct" class="text-lg font-black" style="color:{{ meta.color_from }};">{{ subject_pct }}%</span>
        </div>
        <div class="w-full rounded-full h-2" style="background:rgba(255,255,255,0.07);">
          <div id="subject-bar" class="h-2 rounded-full" style="width:{{ subject_pct }}%;background:linear-gradient(90deg,{{ meta.color_from }},{{ meta.color_to }});transition:width 0.6s ease;"></div>
        </div>
        <div class="flex justify-between mt-2 flex-wrap gap-1">
          <span class="text-xs text-muted">{{ completed_circles }}/{{ total_circles }} units</span>
          <div class="flex items-center gap-2 text-xs text-muted flex-wrap">
            {% for label in circle_labels %}
            <span class="flex items-center gap-1">
              <span class="w-1.5 h-1.5 rounded-full inline-block" style="background:linear-gradient(135deg,{{ meta.color_from }},{{ meta.color_to }});"></span>{{ label }}
            </span>
            {% endfor %}
          </div>
        </div>
      </div>

      {% if exam_plan %}
      <a href="/exam-zone" class="card-base block p-3 mb-4 fade-in" style="animation-delay:0.11s;background:linear-gradient(135deg,rgba(255,107,107,0.08),{{ meta.color_from }}0D);border-color:rgba(255,107,107,0.2);">
        <div class="flex items-center gap-3">
          <div class="text-2xl">📅</div>
          <div class="flex-1">
            <div class="text-xs text-muted font-semibold uppercase tracking-wider">Exam · {{ exam_plan.exam_date_str }}</div>
            <div class="text-sm font-bold text-white">
              {% if exam_plan.days_left < 0 %}<span style="color:#FF6B6B;">Exam passed</span>
              {% elif exam_plan.days_left == 0 %}<span style="color:#FF6B6B;">Today is exam day! 🎯</span>
              {% else %}<span style="color:#FF6B6B;">{{ exam_plan.days_left }} day{{ 's' if exam_plan.days_left != 1 else '' }} left</span> · Study <span style="color:#00E1FD;">{{ exam_plan.per_day_units }} unit{{ 's' if exam_plan.per_day_units != 1 else '' }}/day</span>
              {% endif %}
            </div>
          </div>
          <div style="color:{{ meta.color_from }};">→</div>
        </div>
      </a>
      {% endif %}

      <div class="flex gap-2 mb-4 fade-in" style="animation-delay:0.12s;">
        <button onclick="openAddChapter()" class="btn-secondary flex-1 flex items-center justify-center gap-1.5">
          <span style="font-size:16px;">+</span> Add {% if info.is_custom %}Chapter{% else %}Custom Chapter{% endif %}
        </button>
        {% if info.is_custom %}
        <button onclick="deleteSubject()" class="btn-secondary btn-danger flex items-center justify-center gap-1.5 px-4" title="Delete subject">🗑️</button>
        {% endif %}
      </div>

      {% if not info.is_custom %}
      <p class="text-xs text-muted mb-3 px-1">💡 Default chapters can't be removed, but you can add your own extras below.</p>
      {% endif %}

      {% if chapters | length == 0 %}
      <div class="card-base p-8 text-center fade-in">
        <div class="text-4xl mb-3">📭</div>
        <h3 class="text-base font-bold text-white mb-1">No chapters yet</h3>
        <p class="text-xs text-muted mb-4">Add your first chapter to start tracking progress.</p>
        <button onclick="openAddChapter()" class="gradient-btn px-5 py-2.5 rounded-xl text-white font-bold text-sm">+ Add First Chapter</button>
      </div>
      {% endif %}

      <div class="space-y-2">
        {% for chapter in chapters %}
        {% set ci = loop.index0 %}
        {% set filled_count = chapter_fill_counts[ci] %}
        {% set ch_pct = ((filled_count / 5) * 100) | int %}
        <div class="chapter-row p-4 fade-in" data-chapter="{{ ci }}"
             style="animation-delay:{{ 0.13 + ci * 0.03 }}s;{% if ch_pct == 100 %}border-color:rgba(0,242,96,0.3);background:rgba(0,242,96,0.04);{% endif %}">
          <div class="flex items-start justify-between gap-2 mb-3">
            <div class="flex items-start gap-2.5 flex-1 min-w-0">
              <span class="text-xs font-bold mt-0.5 flex-shrink-0" style="color:{{ meta.color_from }};opacity:0.7;">{{ '%02d' % (ci+1) }}</span>
              <span class="text-sm font-semibold text-white leading-snug">{{ chapter }}</span>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              <span class="chapter-pct text-xs font-bold" style="color:{% if ch_pct == 100 %}#00F260{% else %}{{ meta.color_from }}{% endif %};">
                {% if ch_pct == 100 %}✓ Done{% else %}{{ ch_pct }}%{% endif %}
              </span>
              {% if info.is_custom %}
              <button onclick="deleteChapter({{ ci }}, {{ chapter | tojson }})" class="text-muted hover:text-red-400 text-xs" title="Delete chapter">🗑️</button>
              {% endif %}
            </div>
          </div>
          <div class="flex items-center gap-3">
            <div class="flex items-center gap-2 flex-1 flex-wrap">
              {% for circle_i in range(5) %}
              {% set is_filled = progress_map.get(subject + '__' + ci|string + '__' + circle_i|string, False) %}
              <div class="circle-btn relative w-10 h-10 rounded-full flex items-center justify-center border"
                   data-filled="{{ 'true' if is_filled else 'false' }}"
                   onclick="toggleCircle({{ ci }}, {{ circle_i }}, this)"
                   style="{% if is_filled %}background:linear-gradient(135deg,#00E1FD,#C533FF);border-color:transparent;box-shadow:0 0 12px rgba(0,225,253,0.5);{% else %}background:rgba(255,255,255,0.04);border-color:rgba(255,255,255,0.12);{% endif %}">
                <span class="circle-check absolute text-white font-bold" style="font-size:14px;opacity:{% if is_filled %}1{% else %}0{% endif %};transition:opacity 0.2s;">✓</span>
                <span class="circle-label absolute text-center font-semibold" style="font-size:8px;color:#8892A4;opacity:{% if is_filled %}0{% else %}1{% endif %};transition:opacity 0.2s;line-height:1.1;">{{ circle_labels[circle_i] }}</span>
              </div>
              {% endfor %}
            </div>
            <div class="w-16 flex-shrink-0">
              <div class="w-full rounded-full h-1" style="background:rgba(255,255,255,0.07);">
                <div class="chapter-bar h-1 rounded-full" style="width:{{ ch_pct }}%;background:linear-gradient(90deg,{{ meta.color_from }},{{ meta.color_to }});transition:width 0.4s ease;"></div>
              </div>
            </div>
          </div>
        </div>
        {% endfor %}
      </div>
      <div class="h-6"></div>
    </div>
  </div>

  <div id="addChapterModal" class="modal-overlay">
    <div class="modal-content p-6 fade-in">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-lg font-black text-white">Add New Chapter</h2>
        <button onclick="closeAddChapter()" class="w-8 h-8 rounded-lg flex items-center justify-center" style="background:rgba(255,255,255,0.05);">✕</button>
      </div>
      <form method="POST" action="/add_chapter" class="space-y-4">
        <input type="hidden" name="subject_name" value="{{ subject }}"/>
        <div>
          <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Chapter / Topic Name</label>
          <input type="text" name="name" placeholder="e.g. Newton's Laws of Motion" required maxlength="200" class="input-field w-full px-4 py-3 text-sm font-medium" autofocus/>
        </div>
        <p class="text-xs text-muted">💡 Add topics, chapters, or study points. Each gets 5 progress trackers.</p>
        <button type="submit" class="gradient-btn w-full py-3 rounded-xl text-white font-bold text-sm">➕ Add Chapter</button>
      </form>
    </div>
  </div>
  <script>
    document.getElementById('addChapterModal').addEventListener('click', e => {
      if (e.target.id === 'addChapterModal') closeAddChapter();
    });
  </script>
  """ + presence_script() + """
  {{ nav_html | safe }}
</body></html>
"""


CHAT_TEMPLATE = """
<!DOCTYPE html><html lang="en"><head>
<title>Live Chat — StudyTracker</title>
""" + BASE_STYLE + """
<script>
const CURRENT_USER_ID = {{ user.id }};
const CURRENT_USER_NAME = {{ user.name | tojson }};
const CURRENT_ROOM = {{ current_room | tojson }};
let replyToId = null;
let replyToUser = null;
let typingTimeout = null;
let socket = null;
let seenMessageIds = new Set({{ initial_msg_ids | tojson }});

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function initials(name) {
  const parts = String(name || '?').trim().split(/\\s+/);
  if (parts.length === 1) return parts[0].substring(0,2).toUpperCase();
  return (parts[0][0] + parts[parts.length-1][0]).toUpperCase();
}
function renderMessage(m, animate=true) {
  if (seenMessageIds.has(m.id)) return;
  seenMessageIds.add(m.id);
  const isMine = m.user_id === CURRENT_USER_ID;
  const container = document.createElement('div');
  container.className = 'flex gap-2 mb-3 ' + (animate ? 'msg-in ' : '') + (isMine ? 'flex-row-reverse' : '');
  container.setAttribute('data-msg-id', m.id);

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.style.background = `linear-gradient(135deg, ${m.user_avatar_color || '#00E1FD'}, #C533FF)`;
  avatar.textContent = initials(m.user_name);

  const bubbleWrap = document.createElement('div');
  bubbleWrap.className = 'flex flex-col ' + (isMine ? 'items-end' : 'items-start');
  bubbleWrap.style.maxWidth = 'calc(100% - 50px)';

  const meta = document.createElement('div');
  meta.className = 'text-xs text-muted mb-1 px-1 flex items-center gap-1.5';
  meta.innerHTML = isMine
    ? `<span>${esc(m.time_ago)}</span><span class="font-semibold" style="color:#00E1FD">You</span>`
    : `<span class="font-semibold text-white">${esc(m.user_name)}</span><span class="text-[10px] px-1.5 py-0.5 rounded-full" style="background:rgba(0,225,253,0.1);color:#00E1FD;">${esc(m.user_class)}</span><span>${esc(m.time_ago)}</span>`;

  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + (isMine ? 'mine' : 'theirs');
  let replyHtml = '';
  if (m.reply_to) {
    replyHtml = `<div class="reply-preview" style="margin-bottom:6px;">
      <div><span style="color:#00E1FD;font-weight:700;">↳ ${esc(m.reply_to.user_name)}:</span> <span style="color:#8892A4;">${esc(m.reply_to.message.substring(0,60))}${m.reply_to.message.length > 60 ? '…' : ''}</span></div>
    </div>`;
  }
  bubble.innerHTML = replyHtml + esc(m.message);
  bubble.style.cursor = 'pointer';
  bubble.title = 'Tap to reply';
  bubble.addEventListener('click', () => setReply(m.id, m.user_name, m.message));

  bubbleWrap.appendChild(meta);
  bubbleWrap.appendChild(bubble);
  container.appendChild(avatar);
  container.appendChild(bubbleWrap);

  const scroll = document.getElementById('chatScroll');
  const empty = document.getElementById('emptyState');
  if (empty) empty.remove();
  scroll.appendChild(container);
}
function setReply(msgId, userName, message) {
  replyToId = msgId; replyToUser = userName;
  const preview = document.getElementById('replyPreview');
  preview.style.display = 'flex';
  preview.querySelector('.reply-text').innerHTML = `<span style="color:#00E1FD;font-weight:700;">↳ ${esc(userName)}:</span> <span style="color:#8892A4;">${esc(message.substring(0,60))}${message.length > 60 ? '…' : ''}</span>`;
  document.getElementById('msgInput').focus();
}
function cancelReply() {
  replyToId = null; replyToUser = null;
  document.getElementById('replyPreview').style.display = 'none';
}
function sendMessage() {
  const input = document.getElementById('msgInput');
  const text = input.value.trim();
  if (!text || !socket || !socket.connected) return;
  input.value = '';
  socket.emit('send_message', {
    room: CURRENT_ROOM, message: text, reply_to_id: replyToId
  });
  cancelReply();
  input.focus();
}
function scrollToBottom() {
  const s = document.getElementById('chatScroll');
  s.scrollTop = s.scrollHeight;
}
function isNearBottom() {
  const s = document.getElementById('chatScroll');
  return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
}
function showToast(msg, color='#00E1FD') {
  const t = document.createElement('div');
  t.className = 'join-toast';
  t.style.borderColor = color;
  t.style.color = color;
  t.innerHTML = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}
function updateOnlineUI(data) {
  document.querySelectorAll('.js-online-count').forEach(el => el.textContent = data.online);
  if (data.room_online !== undefined) {
    const roomEl = document.getElementById('roomOnlineCount');
    if (roomEl) roomEl.textContent = data.room_online;
  }
  if (data.users) {
    const strip = document.getElementById('onlineUsersStrip');
    if (strip) {
      strip.innerHTML = '';
      data.users.slice(0, 8).forEach(u => {
        const div = document.createElement('div');
        div.className = 'avatar';
        div.style.cssText = `width:26px;height:26px;font-size:9px;background:linear-gradient(135deg,${u.avatar_color},#C533FF);border:2px solid #0B0F19;`;
        div.title = `${u.name} (${u.student_class})`;
        div.textContent = initials(u.name);
        strip.appendChild(div);
      });
      if (data.users.length > 8) {
        const more = document.createElement('div');
        more.className = 'avatar';
        more.style.cssText = 'width:26px;height:26px;font-size:9px;background:rgba(255,255,255,0.1);border:2px solid #0B0F19;color:#E2E8F0;';
        more.textContent = '+' + (data.users.length - 8);
        strip.appendChild(more);
      }
    }
  }
}
function updateStatus(state) {
  const dot = document.getElementById('connStatus');
  const label = document.getElementById('connLabel');
  if (!dot) return;
  dot.className = 'status-dot ' + state;
  if (label) {
    label.textContent = state === 'connected' ? 'Live' : (state === 'connecting' ? 'Connecting...' : 'Offline');
  }
}
function showTyping(userName) {
  const el = document.getElementById('typingIndicator');
  el.innerHTML = `<span class="font-semibold text-white">${esc(userName)}</span> is typing<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>`;
  el.style.display = 'flex';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.display = 'none'; }, 3000);
}

document.addEventListener('DOMContentLoaded', () => {
  const initial = {{ messages | tojson }};
  initial.forEach(m => renderMessage(m, false));
  scrollToBottom();

  updateStatus('connecting');
  socket = io({transports: ['websocket','polling']});
  
  socket.on('connect', () => {
    updateStatus('connected');
    socket.emit('join_chat_room', {room: CURRENT_ROOM});
  });
  socket.on('disconnect', () => updateStatus('disconnected'));
  socket.on('connect_error', () => updateStatus('disconnected'));
  
  socket.on('new_message', (m) => {
    if (m.room !== CURRENT_ROOM) return;
    const wasAtBottom = isNearBottom();
    renderMessage(m);
    if (wasAtBottom || m.user_id === CURRENT_USER_ID) scrollToBottom();
  });
  
  socket.on('presence_update', updateOnlineUI);
  
  socket.on('user_joined', (data) => {
    if (data.user_id !== CURRENT_USER_ID) {
      showToast(`👋 <strong>${esc(data.name)}</strong> joined ${esc(data.room_name)}`, '#00F260');
    }
  });
  socket.on('user_left', (data) => {
    if (data.user_id !== CURRENT_USER_ID) {
      showToast(`👋 <strong>${esc(data.name)}</strong> left`, '#8892A4');
    }
  });
  socket.on('user_typing', (data) => {
    if (data.user_id !== CURRENT_USER_ID && data.room === CURRENT_ROOM) showTyping(data.name);
  });

  const input = document.getElementById('msgInput');
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  input.addEventListener('input', () => {
    if (!socket || !socket.connected) return;
    if (typingTimeout) return;
    socket.emit('typing', {room: CURRENT_ROOM});
    typingTimeout = setTimeout(() => { typingTimeout = null; }, 2000);
  });
});
</script>
</head><body>
  <div class="fixed pointer-events-none" style="top:-80px;right:-80px;width:350px;height:350px;background:radial-gradient(circle,rgba(0,225,253,0.06) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>

  <div class="relative z-10">
    <div class="max-w-2xl mx-auto">
      <div class="px-4 pt-6 pb-3">
        <div class="flex items-center gap-3 mb-4 fade-in">
          <a href="/" class="w-10 h-10 rounded-xl flex items-center justify-center" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#8892A4" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
          </a>
          <div class="flex-1">
            <div class="flex items-center gap-2">
              <span class="text-2xl">💬</span>
              <h1 class="text-xl font-black text-white">Live Chat</h1>
              <div class="flex items-center gap-1.5 ml-1">
                <span id="connStatus" class="status-dot connecting"></span>
                <span id="connLabel" class="text-xs text-muted">Connecting...</span>
              </div>
            </div>
            <div class="flex items-center gap-2 mt-0.5 text-xs">
              <span class="inline-block w-2 h-2 rounded-full online-pulse" style="background:#00F260;"></span>
              <span style="color:#00F260;"><span class="js-online-count">{{ online_count }}</span> online</span>
              <span class="text-muted">·</span>
              <span class="text-muted"><span id="roomOnlineCount">{{ room_online }}</span> in this room</span>
            </div>
          </div>
        </div>

        <div class="flex gap-2 overflow-x-auto pb-2 fade-in" style="animation-delay:0.05s;scrollbar-width:none;-ms-overflow-style:none;">
          <style>.tabs::-webkit-scrollbar{display:none}</style>
          <div class="tabs flex gap-2">
            {% for room in chat_rooms %}
            <a href="/chat?room={{ room.id }}" class="room-pill flex items-center gap-1.5 px-3 py-2 rounded-full text-xs font-semibold whitespace-nowrap {% if room.id == current_room %}active{% endif %}"
               style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#8892A4;">
              <span>{{ room.icon }}</span><span>{{ room.name }}</span>
            </a>
            {% endfor %}
          </div>
        </div>

        <div class="flex items-center gap-2 mt-3 fade-in" style="animation-delay:0.08s;">
          <span class="text-xs text-muted flex-shrink-0">👥 Online:</span>
          <div id="onlineUsersStrip" class="flex -space-x-2">
            {% for ou in online_users[:8] %}
            <div class="avatar" style="width:26px;height:26px;font-size:9px;background:linear-gradient(135deg,{{ ou.avatar_color }},#C533FF);border:2px solid #0B0F19;" title="{{ ou.name }} ({{ ou.student_class }})">
              {{ ou.name | initials }}
            </div>
            {% endfor %}
            {% if online_users | length > 8 %}
            <div class="avatar" style="width:26px;height:26px;font-size:9px;background:rgba(255,255,255,0.1);border:2px solid #0B0F19;color:#E2E8F0;">+{{ online_users|length - 8 }}</div>
            {% endif %}
          </div>
        </div>
      </div>

      <div id="chatScroll" class="chat-scroll">
        {% if messages | length == 0 %}
        <div class="flex flex-col items-center justify-center h-full text-center px-6" id="emptyState">
          <div class="text-5xl mb-3">👋</div>
          <h3 class="text-base font-bold text-white mb-1">Be the first to say hi!</h3>
          <p class="text-xs text-muted max-w-xs">No messages in <span class="font-semibold text-white">{{ current_room_name }}</span> yet. Start the conversation and help fellow learners.</p>
        </div>
        {% endif %}
      </div>
    </div>
  </div>

  <div class="chat-input-bar">
    <div class="max-w-2xl mx-auto">
      <div id="typingIndicator" class="text-xs text-muted mb-1.5 px-2 items-center gap-1" style="display:none;"></div>
      <div id="replyPreview" class="reply-preview" style="display:none;">
        <div class="reply-text flex-1 min-w-0"></div>
        <button onclick="cancelReply()" class="text-muted hover:text-white ml-2 flex-shrink-0" style="font-size:16px;">✕</button>
      </div>
      <div class="flex items-end gap-2">
        <textarea id="msgInput" placeholder="Type a message..." rows="1" maxlength="1000"
                  class="input-field flex-1 px-4 py-3 text-sm font-medium resize-none" style="max-height:100px;"></textarea>
        <button id="sendBtn" onclick="sendMessage()" class="gradient-btn w-11 h-11 rounded-xl text-white font-bold flex items-center justify-center flex-shrink-0" title="Send">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        </button>
      </div>
    </div>
  </div>

  {{ nav_html | safe }}
</body></html>
"""


EXAM_ZONE_TEMPLATE = """
<!DOCTYPE html><html lang="en"><head>
<title>Exam Zone — StudyTracker</title>
""" + BASE_STYLE + """
<script>
async function deleteExam(examId) {
  if (!confirm('Remove this exam schedule?')) return;
  const resp = await fetch('/delete_exam', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({exam_id: examId})
  });
  const data = await resp.json();
  if (data.success) location.reload();
  else alert('Failed');
}
function openScheduleModal() { document.getElementById('scheduleModal').classList.add('active'); }
function closeScheduleModal() { document.getElementById('scheduleModal').classList.remove('active'); }
</script>
</head><body>
  <div class="fixed pointer-events-none" style="top:-100px;right:-100px;width:400px;height:400px;background:radial-gradient(circle,rgba(255,107,107,0.08) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>
  <div class="relative z-10 scroll-content">
    <div class="max-w-2xl mx-auto px-4 pt-8 pb-4">
      <div class="flex items-center gap-4 mb-6 fade-in">
        <a href="/" class="w-10 h-10 rounded-xl flex items-center justify-center" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#8892A4" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
        </a>
        <div class="flex-1">
          <div class="flex items-center gap-2">
            <span class="text-2xl">📅</span>
            <h1 class="text-2xl font-black text-white">Exam Zone</h1>
          </div>
          <p class="text-xs text-muted">Plan your syllabus around exam dates</p>
        </div>
      </div>

      <div class="grid grid-cols-3 gap-3 mb-4 fade-in" style="animation-delay:0.08s;">
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1">📆</div>
          <div class="text-xl font-black text-white">{{ exam_plans | length }}</div>
          <div class="text-xs text-muted leading-tight mt-0.5">Exams<br/>Scheduled</div>
        </div>
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1">⏳</div>
          <div class="text-xl font-black" style="color:#FF6B6B;">
            {% if nearest_days is not none %}{{ nearest_days }}{% else %}—{% endif %}
          </div>
          <div class="text-xs text-muted leading-tight mt-0.5">Nearest<br/>Exam Day{{ 's' if nearest_days != 1 else '' }}</div>
        </div>
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1">🎯</div>
          <div class="text-xl font-black gradient-text">{{ today_target }}</div>
          <div class="text-xs text-muted leading-tight mt-0.5">Units<br/>Today</div>
        </div>
      </div>

      <button onclick="openScheduleModal()" class="gradient-btn w-full py-3.5 rounded-xl text-white font-bold text-sm mb-5 fade-in" style="animation-delay:0.1s;">
        + Schedule New Exam
      </button>

      {% if exam_plans | length == 0 %}
      <div class="card-base p-8 text-center fade-in" style="animation-delay:0.15s;">
        <div class="text-5xl mb-3">🗓️</div>
        <h3 class="text-lg font-black text-white mb-1">No Exams Scheduled</h3>
        <p class="text-sm text-muted mb-4 max-w-xs mx-auto">Add your first exam date and get a personalized daily study target.</p>
        <button onclick="openScheduleModal()" class="gradient-btn px-6 py-3 rounded-xl text-white font-bold text-sm">📅 Schedule First Exam</button>
      </div>
      {% else %}
      <h2 class="text-sm font-bold text-white mb-3 fade-in" style="animation-delay:0.12s;">Your Study Plan</h2>
      <div class="space-y-3">
        {% for plan in exam_plans %}
        {% set meta = plan.meta %}
        <div class="card-base exam-card fade-in" style="animation-delay:{{ 0.15 + loop.index0 * 0.06 }}s;
             {% if plan.feasibility == 'urgent' %}border-color:rgba(255,60,60,0.4);
             {% elif plan.feasibility == 'tough' %}border-color:rgba(255,180,50,0.35);
             {% elif plan.feasibility == 'complete' %}border-color:rgba(0,242,96,0.35);
             {% elif plan.feasibility == 'expired' %}opacity:0.55;
             {% endif %}">
          <div class="absolute -top-10 -right-10 w-32 h-32 rounded-full opacity-10 pointer-events-none" style="background:radial-gradient(circle,{{ meta.color_from }} 0%,transparent 70%);filter:blur(30px);"></div>
          <div class="flex items-start justify-between gap-3 mb-3 relative">
            <div class="flex items-start gap-3 flex-1 min-w-0">
              <div class="w-11 h-11 rounded-xl flex items-center justify-center text-xl flex-shrink-0" style="background:linear-gradient(135deg,{{ meta.color_from }}22,{{ meta.color_to }}22);border:1px solid {{ meta.color_from }}33;">{{ meta.icon }}</div>
              <div class="flex-1 min-w-0">
                <h3 class="text-base font-black text-white truncate">{{ plan.subject }}</h3>
                <div class="text-xs text-muted mt-0.5">📅 {{ plan.exam_day }}, {{ plan.exam_date_str }}</div>
              </div>
            </div>
            <div class="text-right flex-shrink-0">
              {% if plan.status == 'past' %}
                <span class="countdown-badge" style="background:rgba(150,150,150,0.15);color:#8892A4;">PAST</span>
              {% elif plan.status == 'today' %}
                <span class="countdown-badge" style="background:rgba(255,60,60,0.15);color:#FF6B6B;">TODAY!</span>
              {% else %}
                <span class="countdown-badge" style="background:{% if plan.days_left <= 7 %}rgba(255,60,60,0.15);color:#FF6B6B{% elif plan.days_left <= 21 %}rgba(255,180,50,0.15);color:#FFD700{% else %}rgba(0,242,96,0.15);color:#00F260{% endif %};">
                  ⏳ {{ plan.days_left }} DAY{{ 'S' if plan.days_left != 1 else '' }}
                </span>
              {% endif %}
              <button onclick="deleteExam({{ plan.id }})" class="text-xs text-muted mt-1 opacity-60 hover:opacity-100 block ml-auto" title="Delete">🗑️</button>
            </div>
          </div>
          <div class="mb-3">
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs text-muted">Syllabus completion</span>
              <span class="text-xs font-bold" style="color:{{ meta.color_from }};">{{ plan.progress_pct }}%</span>
            </div>
            <div class="w-full rounded-full h-2" style="background:rgba(255,255,255,0.07);">
              <div class="h-2 rounded-full" style="width:{{ plan.progress_pct }}%;background:linear-gradient(90deg,{{ meta.color_from }},{{ meta.color_to }});transition:width 0.8s ease;"></div>
            </div>
          </div>
          {% if plan.feasibility == 'complete' %}
          <div class="rounded-xl p-3 flex items-center gap-3" style="background:rgba(0,242,96,0.08);border:1px solid rgba(0,242,96,0.25);">
            <div class="text-2xl">🎉</div>
            <div>
              <div class="text-sm font-bold" style="color:#00F260;">Syllabus Complete!</div>
              <div class="text-xs text-muted">You're 100% ready. Just revise now.</div>
            </div>
          </div>
          {% elif plan.status == 'past' %}
          <div class="rounded-xl p-3 text-center" style="background:rgba(150,150,150,0.06);">
            <div class="text-xs text-muted">Exam date has passed</div>
          </div>
          {% else %}
          <div class="grid grid-cols-3 gap-2 mb-3">
            <div class="rounded-xl p-3 text-center" style="background:rgba(0,225,253,0.08);border:1px solid rgba(0,225,253,0.2);">
              <div class="text-xl font-black" style="color:#00E1FD;">{{ plan.per_day_units }}</div>
              <div class="text-muted leading-tight mt-0.5" style="font-size:10px;">Units<br/>per day</div>
            </div>
            <div class="rounded-xl p-3 text-center" style="background:rgba(197,51,255,0.08);border:1px solid rgba(197,51,255,0.2);">
              <div class="text-xl font-black" style="color:#C533FF;">{{ plan.per_day_chapters }}</div>
              <div class="text-muted leading-tight mt-0.5" style="font-size:10px;">Chapters<br/>per day</div>
            </div>
            <div class="rounded-xl p-3 text-center" style="background:rgba(0,242,96,0.08);border:1px solid rgba(0,242,96,0.2);">
              <div class="text-xl font-black" style="color:#00F260;">{{ plan.per_week_chapters }}</div>
              <div class="text-muted leading-tight mt-0.5" style="font-size:10px;">Chapters<br/>per week</div>
            </div>
          </div>
          <div class="flex items-center justify-between text-xs text-muted pt-2 border-t" style="border-color:rgba(255,255,255,0.05);">
            <span>{{ plan.remaining_chapters }}/{{ plan.total_chapters }} chapters left</span>
            <span>{{ plan.remaining_units }}/{{ plan.total_units }} units left</span>
          </div>
          <div class="mt-3">
            {% if plan.feasibility == 'easy' %}
            <div class="text-xs font-semibold px-3 py-2 rounded-lg" style="background:rgba(0,242,96,0.1);color:#00F260;border:1px solid rgba(0,242,96,0.25);">✅ Easy pace — you're in great shape!</div>
            {% elif plan.feasibility == 'moderate' %}
            <div class="text-xs font-semibold px-3 py-2 rounded-lg" style="background:rgba(0,225,253,0.1);color:#00E1FD;border:1px solid rgba(0,225,253,0.25);">💪 Moderate pace — stay consistent</div>
            {% elif plan.feasibility == 'tough' %}
            <div class="text-xs font-semibold px-3 py-2 rounded-lg" style="background:rgba(255,180,50,0.1);color:#FFD700;border:1px solid rgba(255,180,50,0.3);">⚡ Tough pace — buckle down!</div>
            {% elif plan.feasibility == 'urgent' %}
            <div class="text-xs font-semibold px-3 py-2 rounded-lg" style="background:rgba(255,60,60,0.1);color:#FF6B6B;border:1px solid rgba(255,60,60,0.3);">🚨 Urgent! Consider prioritizing key topics</div>
            {% endif %}
          </div>
          {% endif %}
          <a href="/subject/{{ plan.subject | urlencode }}" class="block mt-3 text-center py-2 rounded-lg text-xs font-bold" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:{{ meta.color_from }};">
            Open {{ plan.subject }} →
          </a>
        </div>
        {% endfor %}
      </div>
      {% endif %}
    </div>
  </div>

  <div id="scheduleModal" class="modal-overlay">
    <div class="modal-content p-6 fade-in">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-lg font-black text-white">Schedule Exam 📅</h2>
        <button onclick="closeScheduleModal()" class="w-8 h-8 rounded-lg flex items-center justify-center" style="background:rgba(255,255,255,0.05);">✕</button>
      </div>
      <form method="POST" action="/add_exam" class="space-y-4">
        <div>
          <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Select Subject</label>
          <select name="subject" required class="input-field w-full px-4 py-3 text-sm font-medium">
            <option value="">— Choose a subject —</option>
            {% for subject in all_subjects %}
            <option value="{{ subject }}">{{ subject }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Exam Date</label>
          <input type="date" name="exam_date" required min="{{ today_iso }}" class="input-field w-full px-4 py-3 text-sm font-medium"/>
        </div>
        <p class="text-xs text-muted">💡 Your daily study target will be calculated automatically.</p>
        <button type="submit" class="gradient-btn w-full py-3 rounded-xl text-white font-bold text-sm">📌 Schedule Exam</button>
      </form>
    </div>
  </div>

  <script>
    document.getElementById('scheduleModal').addEventListener('click', e => {
      if (e.target.id === 'scheduleModal') closeScheduleModal();
    });
  </script>
  """ + presence_script() + """
  {{ nav_html | safe }}
</body></html>
"""


ANALYTICS_TEMPLATE = """
<!DOCTYPE html><html lang="en"><head>
<title>Analytics — StudyTracker</title>
""" + BASE_STYLE + """
</head><body>
  <div class="fixed pointer-events-none" style="top:-80px;left:-80px;width:400px;height:400px;background:radial-gradient(circle,rgba(0,225,253,0.06) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>
  <div class="relative z-10 scroll-content">
    <div class="max-w-2xl mx-auto px-4 pt-8 pb-4">
      <div class="flex items-center gap-4 mb-6 fade-in">
        <a href="/" class="w-10 h-10 rounded-xl flex items-center justify-center" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#8892A4" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
        </a>
        <div>
          <h1 class="text-2xl font-black text-white">Analytics</h1>
          <p class="text-xs text-muted">Detailed performance overview</p>
        </div>
      </div>

      <div class="card-base p-5 mb-4 fade-in" style="animation-delay:0.08s;">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-sm font-bold text-white">7-Day Activity</h2>
            <p class="text-xs text-muted">Total: {{ week_total }} units · Avg: {{ week_avg }}/day</p>
          </div>
          <div class="w-9 h-9 rounded-xl flex items-center justify-center" style="background:linear-gradient(135deg,#00E1FD22,#C533FF22);border:1px solid rgba(0,225,253,0.25);">📈</div>
        </div>
        <div class="flex items-end justify-between gap-2" style="height:150px;">
          {% for day in week_data %}
          {% set bar_height = (day.count / week_max * 100) if week_max > 0 else 0 %}
          <div class="flex flex-col items-center flex-1 gap-1.5">
            <div class="w-full flex flex-col justify-end items-center" style="height:115px;">
              {% if day.count > 0 %}
              <span class="text-xs font-bold mb-1" style="color:{% if day.is_today %}#00E1FD{% else %}#E2E8F0{% endif %};">{{ day.count }}</span>
              {% endif %}
              <div class="bar-chart-bar w-full rounded-t-lg" style="
                height:{{ bar_height if bar_height >= 6 else (6 if day.count > 0 else 3) }}%;
                background:{% if day.is_today %}linear-gradient(180deg,#00E1FD,#C533FF){% elif day.count > 0 %}linear-gradient(180deg,rgba(0,225,253,0.6),rgba(197,51,255,0.4)){% else %}rgba(255,255,255,0.06){% endif %};
                min-height:4px;
                box-shadow:{% if day.is_today %}0 0 12px rgba(0,225,253,0.4){% else %}none{% endif %};
              "></div>
            </div>
            <div class="text-center">
              <div class="text-xs font-semibold" style="color:{% if day.is_today %}#00E1FD{% else %}#8892A4{% endif %};">{{ day.day_label }}</div>
              <div class="text-muted" style="font-size:9px;">{{ day.date }}</div>
            </div>
          </div>
          {% endfor %}
        </div>
      </div>

      <div class="card-base p-5 mb-4 fade-in" style="animation-delay:0.1s;">
        <h2 class="text-sm font-bold text-white mb-4">Overall Sync Breakdown</h2>
        <div class="flex items-center gap-6">
          <div class="relative flex-shrink-0" style="width:90px;height:90px;">
            <svg width="90" height="90" viewBox="0 0 90 90" style="transform:rotate(-90deg);">
              <circle cx="45" cy="45" r="36" fill="none" stroke="rgba(255,255,255,0.07)" stroke-width="9"/>
              <circle cx="45" cy="45" r="36" fill="none" stroke="url(#ag)" stroke-width="9" stroke-linecap="round"
                stroke-dasharray="{{ ring_dash_small }} {{ ring_gap_small }}"/>
              <defs><linearGradient id="ag" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" style="stop-color:#00E1FD"/><stop offset="100%" style="stop-color:#C533FF"/></linearGradient></defs>
            </svg>
            <div class="absolute inset-0 flex flex-col items-center justify-center">
              <span class="text-lg font-black text-white">{{ stats.overall_pct }}%</span>
            </div>
          </div>
          <div class="flex-1 space-y-2">
            <div class="flex justify-between text-sm"><span class="text-muted">Completed Units</span><span class="font-bold text-white">{{ stats.completed_circles }}</span></div>
            <div class="flex justify-between text-sm"><span class="text-muted">Total Units</span><span class="font-bold text-white">{{ stats.total_circles }}</span></div>
            <div class="flex justify-between text-sm"><span class="text-muted">Study Streak</span><span class="font-bold" style="color:#FF6B6B;">{{ user.streak or 0 }} 🔥</span></div>
            <div class="flex justify-between text-sm"><span class="text-muted">Chapters Done</span><span class="font-bold" style="color:#00F260;">{{ stats.fully_done_chapters }}/{{ stats.total_chapters }}</span></div>
          </div>
        </div>
      </div>

      <div class="card-base p-5 mb-4 fade-in" style="animation-delay:0.12s;background:linear-gradient(135deg,rgba(0,242,96,0.05),rgba(0,225,253,0.05));border-color:rgba(0,242,96,0.15);">
        <h2 class="text-sm font-bold text-white mb-3 flex items-center gap-2">🌐 Community Pulse (Live)</h2>
        <div class="grid grid-cols-2 gap-3">
          <div class="rounded-xl p-3 text-center" style="background:rgba(0,242,96,0.05);border:1px solid rgba(0,242,96,0.15);">
            <div class="flex items-center justify-center gap-1">
              <span class="inline-block w-2 h-2 rounded-full online-pulse" style="background:#00F260;"></span>
              <div class="text-2xl font-black js-online-count" style="color:#00F260;">{{ online_count }}</div>
            </div>
            <div class="text-xs text-muted mt-1">Online Now</div>
          </div>
          <div class="rounded-xl p-3 text-center" style="background:rgba(0,225,253,0.05);border:1px solid rgba(0,225,253,0.15);">
            <div class="text-2xl font-black gradient-text">{{ total_users }}</div>
            <div class="text-xs text-muted mt-1">Total Learners</div>
          </div>
        </div>
      </div>

      <h2 class="text-sm font-bold text-white mb-3 fade-in" style="animation-delay:0.15s;">Subject Performance</h2>
      <div class="space-y-3">
        {% for subject, info in curriculum.items() %}
        {% set meta = info.meta %}
        {% set pct = stats.subject_pcts[subject] %}
        <div class="card-base p-4 fade-in" style="animation-delay:{{ 0.18 + loop.index0 * 0.05 }}s;border-radius:14px;">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-9 h-9 rounded-xl flex items-center justify-center text-lg flex-shrink-0" style="background:linear-gradient(135deg,{{ meta.color_from }}22,{{ meta.color_to }}22);border:1px solid {{ meta.color_from }}33;">{{ meta.icon }}</div>
            <div class="flex-1 min-w-0">
              <div class="flex items-center justify-between">
                <span class="text-sm font-semibold text-white truncate">{{ subject }}</span>
                <span class="text-sm font-black flex-shrink-0 ml-2" style="color:{{ meta.color_from }};">{{ pct }}%</span>
              </div>
              <span class="text-xs text-muted">{{ info.chapters | length }} chapter{{ 's' if info.chapters|length != 1 else '' }}{% if info.is_custom %} · custom{% endif %}</span>
            </div>
          </div>
          <div class="w-full rounded-full h-2" style="background:rgba(255,255,255,0.07);">
            <div class="h-2 rounded-full" style="width:{{ pct }}%;background:linear-gradient(90deg,{{ meta.color_from }},{{ meta.color_to }});transition:width 0.8s ease;"></div>
          </div>
          {% if info.chapters|length > 0 %}
          <div class="flex gap-1 mt-2">
            {% for i in range(info.chapters|length) %}
            {% set ch_done = chapter_done_map[subject][i] %}
            <div class="flex-1 h-1 rounded-full" style="background:{% if ch_done == 5 %}#00F260{% elif ch_done > 0 %}{{ meta.color_from }}{% else %}rgba(255,255,255,0.08){% endif %};opacity:0.9;"></div>
            {% endfor %}
          </div>
          {% endif %}
        </div>
        {% endfor %}
      </div>
    </div>
  </div>
  """ + presence_script() + """
  {{ nav_html | safe }}
</body></html>
"""


SETTINGS_TEMPLATE = """
<!DOCTYPE html><html lang="en"><head>
<title>Settings — StudyTracker</title>
""" + BASE_STYLE + """
</head><body>
  <div class="fixed pointer-events-none" style="top:-80px;right:-80px;width:400px;height:400px;background:radial-gradient(circle,rgba(197,51,255,0.07) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>
  <div class="relative z-10 scroll-content">
    <div class="max-w-2xl mx-auto px-4 pt-8 pb-4">
      <div class="flex items-center gap-4 mb-6 fade-in">
        <a href="/" class="w-10 h-10 rounded-xl flex items-center justify-center" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#8892A4" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
        </a>
        <div>
          <h1 class="text-2xl font-black text-white">Settings</h1>
          <p class="text-xs text-muted">Manage your profile & data</p>
        </div>
      </div>

      <div class="card-base p-6 mb-4 fade-in" style="animation-delay:0.1s;">
        <div class="flex items-center gap-4 mb-4">
          <div class="avatar" style="width:56px;height:56px;font-size:18px;background:linear-gradient(135deg,{{ user.avatar_color }},#C533FF);">{{ user.name | initials }}</div>
          <div>
            <h2 class="text-lg font-black text-white">{{ user.name }}</h2>
            <p class="text-sm text-muted">{{ user.student_class }}</p>
            <p class="text-xs text-muted mt-0.5">Member since {{ member_since }}</p>
          </div>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div class="rounded-xl p-3 text-center" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.07);">
            <div class="text-2xl font-black text-white">{{ user.streak or 0 }}</div>
            <div class="text-xs text-muted">Day Streak 🔥</div>
          </div>
          <div class="rounded-xl p-3 text-center" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.07);">
            <div class="text-2xl font-black gradient-text">{{ overall_pct }}%</div>
            <div class="text-xs text-muted">Overall Sync</div>
          </div>
        </div>
      </div>

      <div class="card-base p-5 mb-4 fade-in" style="animation-delay:0.12s;">
        <h3 class="text-sm font-bold text-white mb-3">Avatar Color</h3>
        <form method="POST" action="/update_avatar" class="flex items-center gap-2 flex-wrap">
          {% for c in avatar_colors %}
          <button type="submit" name="color" value="{{ c }}" class="w-10 h-10 rounded-full transition-all {% if user.avatar_color == c %}ring-4 ring-white{% endif %}" style="background:linear-gradient(135deg,{{ c }},#C533FF);"></button>
          {% endfor %}
        </form>
      </div>

      {% if custom_subjects %}
      <div class="card-base p-5 mb-4 fade-in" style="animation-delay:0.13s;">
        <h3 class="text-sm font-bold text-white mb-3">Your Custom Subjects ({{ custom_subjects | length }})</h3>
        <div class="space-y-2">
          {% for cs in custom_subjects %}
          <div class="chapter-item">
            <div class="flex items-center gap-3 flex-1 min-w-0">
              <span class="text-lg">{{ cs.icon }}</span>
              <div class="flex-1 min-w-0">
                <div class="text-sm font-semibold text-white truncate">{{ cs.name }}</div>
                <div class="text-xs text-muted">{{ cs.chapter_count }} chapter{{ 's' if cs.chapter_count != 1 else '' }}</div>
              </div>
            </div>
            <a href="/subject/{{ cs.name | urlencode }}" class="btn-secondary text-xs">Open →</a>
          </div>
          {% endfor %}
        </div>
      </div>
      {% endif %}

      <div class="card-base p-5 mb-4 fade-in" style="animation-delay:0.15s;">
        <h3 class="text-sm font-bold text-white mb-4">Update Profile</h3>
        {% if msg %}
        <div class="mb-3 p-3 rounded-xl text-sm" style="background:rgba(0,242,96,0.1);border:1px solid rgba(0,242,96,0.3);color:#00F260;">✅ {{ msg }}</div>
        {% endif %}
        <form method="POST" action="/settings" class="space-y-3">
          <div>
            <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-1.5">Name</label>
            <input type="text" name="name" value="{{ user.name }}" required maxlength="100" class="input-field w-full px-4 py-3 text-sm font-medium"/>
          </div>
          <div>
            <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-1.5">Class</label>
            <input type="text" name="student_class" value="{{ user.student_class }}" required maxlength="50" class="input-field w-full px-4 py-3 text-sm font-medium"/>
          </div>
          <button type="submit" class="gradient-btn w-full py-3 rounded-xl text-white font-bold text-sm">Save Changes</button>
        </form>
      </div>

      <div class="card-base p-5 fade-in" style="animation-delay:0.2s;border-color:rgba(255,100,100,0.15);">
        <h3 class="text-sm font-bold mb-1" style="color:#FF6B6B;">Danger Zone</h3>
        <p class="text-xs text-muted mb-4">This will permanently delete all your data.</p>
        <form method="POST" action="/reset" onsubmit="return confirm('Are you sure? All data will be deleted permanently.');">
          <button type="submit" class="w-full py-3 rounded-xl text-sm font-bold border transition-all" style="background:rgba(255,100,100,0.08);border-color:rgba(255,100,100,0.3);color:#FF6B6B;">🗑️ Reset All Data & Start Over</button>
        </form>
      </div>
    </div>
  </div>
  """ + presence_script() + """
  {{ nav_html | safe }}
</body></html>
"""


# ═════════════════════════════════════════════
# HTTP ROUTES
# ═════════════════════════════════════════════

@app.route('/')
def index():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    touch_user(user)
    update_streak(user)
    stats = compute_stats(user.id)
    curriculum = get_full_curriculum(user.id)
    circ = 251.33
    ring_dash = round((stats['overall_pct'] / 100) * circ, 2)
    ring_gap = round(circ - ring_dash, 2)
    week_data = get_7day_data(user.id)
    week_total = sum(d['count'] for d in week_data)
    week_max = max((d['count'] for d in week_data), default=0)
    exam_plans = compute_exam_plans(user.id)
    nearest_exam = None
    for p in exam_plans:
        if p['status'] in ('upcoming', 'today'):
            nearest_exam = p; break
    return render_template_string(
        DASHBOARD_TEMPLATE,
        user=user, stats=stats, curriculum=curriculum,
        ring_dash=ring_dash, ring_gap=ring_gap,
        week_data=week_data, week_total=week_total, week_max=week_max,
        icon_presets=ICON_PRESETS, color_presets=COLOR_PRESETS,
        nearest_exam=nearest_exam,
        online_count=get_live_online_count(),
        total_users=get_total_user_count(),
        nav_html=bottom_nav_html("hub")
    )


@app.route('/onboarding')
def onboarding():
    if get_current_user():
        return redirect(url_for('index'))
    return render_template_string(
        ONBOARDING_TEMPLATE, error=None,
        online_count=get_live_online_count(),
        total_users=get_total_user_count()
    )


@app.route('/setup', methods=['POST'])
def setup():
    name = request.form.get('name', '').strip()
    student_class = request.form.get('student_class', '').strip()
    if not name or not student_class:
        return render_template_string(
            ONBOARDING_TEMPLATE, error="Please fill in all fields.",
            online_count=get_live_online_count(), total_users=get_total_user_count()
        )
    if len(name) > 100 or len(student_class) > 50:
        return render_template_string(
            ONBOARDING_TEMPLATE, error="Input too long.",
            online_count=get_live_online_count(), total_users=get_total_user_count()
        )
    color = AVATAR_COLORS[len(name) % len(AVATAR_COLORS)]
    user = User(name=name, student_class=student_class, streak=1,
                last_active=date.today(), created_at=datetime.utcnow(),
                last_seen=datetime.utcnow(), avatar_color=color)
    db.session.add(user)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return render_template_string(
            ONBOARDING_TEMPLATE, error=f"Setup failed: {str(e)[:100]}",
            online_count=get_live_online_count(), total_users=get_total_user_count()
        )
    session['user_id'] = user.id
    session.permanent = True
    return redirect(url_for('index'))


@app.route('/subject/<path:subject_name>')
def subject_page(subject_name):
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    touch_user(user)
    curriculum = get_full_curriculum(user.id)
    if subject_name not in curriculum:
        return redirect(url_for('index'))
    update_streak(user)
    info = curriculum[subject_name]
    chapters = info['chapters']
    meta = info['meta']
    progress_map = get_user_progress(user.id)
    chapter_fill_counts = {}
    for ci in range(len(chapters)):
        chapter_fill_counts[ci] = sum(
            1 for circle_i in range(CIRCLES_PER_CHAPTER)
            if progress_map.get(f"{subject_name}__{ci}__{circle_i}", False)
        )
    subj_total = len(chapters) * CIRCLES_PER_CHAPTER
    subj_done = sum(chapter_fill_counts.values())
    subject_pct = round((subj_done / subj_total) * 100, 1) if subj_total else 0
    exam_plan = None
    for p in compute_exam_plans(user.id):
        if p['subject'] == subject_name:
            exam_plan = p; break
    return render_template_string(
        SUBJECT_TEMPLATE,
        user=user, subject=subject_name, chapters=chapters, meta=meta, info=info,
        progress_map=progress_map, circle_labels=CIRCLE_LABELS,
        chapter_fill_counts=chapter_fill_counts, subject_pct=subject_pct,
        completed_circles=subj_done, total_circles=subj_total,
        exam_plan=exam_plan,
        nav_html=bottom_nav_html("hub")
    )


@app.route('/add_subject', methods=['POST'])
def add_subject():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    name = request.form.get('name', '').strip()
    if not name:
        return redirect(url_for('index'))
    curriculum = get_full_curriculum(user.id)
    if name in curriculum:
        return redirect(url_for('index'))
    cs = CustomSubject(
        user_id=user.id, name=name[:100],
        icon=request.form.get('icon', '📘')[:10],
        color_from=request.form.get('color_from', '#00E1FD')[:20],
        color_to=request.form.get('color_to', '#C533FF')[:20],
        tag=(request.form.get('tag', '').strip() or 'CUSTOM')[:50]
    )
    db.session.add(cs)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return redirect(url_for('index'))
    return redirect(url_for('subject_page', subject_name=cs.name))


@app.route('/add_chapter', methods=['POST'])
def add_chapter():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    subject_name = request.form.get('subject_name', '').strip()
    name = request.form.get('name', '').strip()
    if not subject_name or not name:
        return redirect(url_for('index'))
    cs = CustomSubject.query.filter_by(user_id=user.id, name=subject_name).first()
    if not cs and subject_name in DEFAULT_CURRICULUM:
        meta = DEFAULT_SUBJECT_META[subject_name]
        cs = CustomSubject(
            user_id=user.id, name=subject_name,
            icon=meta['icon'], color_from=meta['color_from'],
            color_to=meta['color_to'], tag=meta['tag']
        )
        db.session.add(cs)
        try:
            db.session.flush()
            for i, ch_name in enumerate(DEFAULT_CURRICULUM[subject_name]):
                db.session.add(CustomChapter(subject_id=cs.id, name=ch_name, order_index=i))
            db.session.commit()
        except Exception:
            db.session.rollback()
            return redirect(url_for('index'))
    if not cs:
        return redirect(url_for('index'))
    max_order = db.session.query(func.max(CustomChapter.order_index)).filter_by(subject_id=cs.id).scalar() or 0
    ch = CustomChapter(subject_id=cs.id, name=name[:200], order_index=max_order + 1)
    db.session.add(ch)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return redirect(url_for('subject_page', subject_name=cs.name))


@app.route('/delete_chapter', methods=['POST'])
def delete_chapter():
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
    data = request.get_json(silent=True) or {}
    try:
        subject_id = int(data.get('subject_id'))
        chapter_index = int(data.get('chapter_index'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid data'}), 400
    cs = CustomSubject.query.filter_by(id=subject_id, user_id=user.id).first()
    if not cs:
        return jsonify({'success': False, 'error': 'Subject not found'}), 404
    chapters = CustomChapter.query.filter_by(subject_id=cs.id).order_by(CustomChapter.order_index, CustomChapter.id).all()
    if not (0 <= chapter_index < len(chapters)):
        return jsonify({'success': False, 'error': 'Invalid chapter'}), 400
    ChapterProgress.query.filter_by(user_id=user.id, subject=cs.name, chapter_index=chapter_index).delete()
    later_progress = ChapterProgress.query.filter(
        ChapterProgress.user_id == user.id,
        ChapterProgress.subject == cs.name,
        ChapterProgress.chapter_index > chapter_index
    ).all()
    for p in later_progress:
        p.chapter_index -= 1
    db.session.delete(chapters[chapter_index])
    remaining = [c for i, c in enumerate(chapters) if i != chapter_index]
    for new_idx, ch in enumerate(remaining):
        ch.order_index = new_idx
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'DB error'}), 500
    return jsonify({'success': True})


@app.route('/delete_subject', methods=['POST'])
def delete_subject():
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
    data = request.get_json(silent=True) or {}
    try:
        subject_id = int(data.get('subject_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid data'}), 400
    cs = CustomSubject.query.filter_by(id=subject_id, user_id=user.id).first()
    if not cs:
        return jsonify({'success': False, 'error': 'Subject not found'}), 404
    ExamSchedule.query.filter_by(user_id=user.id, subject=cs.name).delete()
    ChapterProgress.query.filter_by(user_id=user.id, subject=cs.name).delete()
    CustomChapter.query.filter_by(subject_id=cs.id).delete()
    db.session.delete(cs)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'DB error'}), 500
    return jsonify({'success': True})


@app.route('/update_progress', methods=['POST'])
def update_progress():
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
    touch_user(user)
    data = request.get_json(silent=True) or {}
    subject = data.get('subject')
    try:
        chapter_index = int(data.get('chapter_index'))
        circle_index = int(data.get('circle_index'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid indices'}), 400
    completed = bool(data.get('completed', False))
    curriculum = get_full_curriculum(user.id)
    if subject not in curriculum:
        return jsonify({'success': False, 'error': 'Invalid subject'}), 400
    if not (0 <= chapter_index < len(curriculum[subject]['chapters'])):
        return jsonify({'success': False, 'error': 'Invalid chapter'}), 400
    if not (0 <= circle_index < CIRCLES_PER_CHAPTER):
        return jsonify({'success': False, 'error': 'Invalid circle'}), 400
    existing = ChapterProgress.query.filter_by(
        user_id=user.id, subject=subject,
        chapter_index=chapter_index, circle_index=circle_index
    ).first()
    delta = 0
    if existing:
        prev = existing.completed
        existing.completed = completed
        existing.updated_at = datetime.utcnow()
        if not prev and completed: delta = 1
        elif prev and not completed: delta = -1
    else:
        db.session.add(ChapterProgress(
            user_id=user.id, subject=subject,
            chapter_index=chapter_index, circle_index=circle_index,
            completed=completed
        ))
        if completed: delta = 1
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'DB error'}), 500
    if delta != 0:
        log_activity(user.id, delta)
    chapters = curriculum[subject]['chapters']
    subj_total = len(chapters) * CIRCLES_PER_CHAPTER
    subj_done = ChapterProgress.query.filter_by(
        user_id=user.id, subject=subject, completed=True).count()
    subject_pct = round((subj_done / subj_total) * 100, 1) if subj_total else 0
    return jsonify({'success': True, 'subject_pct': subject_pct})


@app.route('/exam-zone')
def exam_zone():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    touch_user(user); update_streak(user)
    exam_plans = compute_exam_plans(user.id)
    curriculum = get_full_curriculum(user.id)
    scheduled = {p['subject'] for p in exam_plans}
    all_subjects = [s for s in curriculum.keys() if s not in scheduled]
    today_target = sum(p['per_day_units'] for p in exam_plans if p['status'] in ('upcoming', 'today'))
    nearest_days = None
    for p in exam_plans:
        if p['status'] in ('upcoming', 'today'):
            nearest_days = p['days_left']; break
    return render_template_string(
        EXAM_ZONE_TEMPLATE,
        user=user, exam_plans=exam_plans, all_subjects=all_subjects,
        today_iso=date.today().isoformat(), today_target=today_target,
        nearest_days=nearest_days,
        nav_html=bottom_nav_html("exam")
    )


@app.route('/add_exam', methods=['POST'])
def add_exam():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    subject = request.form.get('subject', '').strip()
    exam_date_str = request.form.get('exam_date', '').strip()
    if not subject or not exam_date_str:
        return redirect(url_for('exam_zone'))
    try:
        exam_dt = datetime.strptime(exam_date_str, '%Y-%m-%d').date()
    except ValueError:
        return redirect(url_for('exam_zone'))
    curriculum = get_full_curriculum(user.id)
    if subject not in curriculum:
        return redirect(url_for('exam_zone'))
    existing = ExamSchedule.query.filter_by(user_id=user.id, subject=subject).first()
    if existing:
        existing.exam_date = exam_dt
    else:
        db.session.add(ExamSchedule(user_id=user.id, subject=subject, exam_date=exam_dt))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return redirect(url_for('exam_zone'))


@app.route('/delete_exam', methods=['POST'])
def delete_exam():
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
    data = request.get_json(silent=True) or {}
    try:
        exam_id = int(data.get('exam_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid'}), 400
    exam = ExamSchedule.query.filter_by(id=exam_id, user_id=user.id).first()
    if not exam:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    db.session.delete(exam)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'DB error'}), 500
    return jsonify({'success': True})


@app.route('/chat')
def chat():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    touch_user(user); update_streak(user)
    room_id = request.args.get('room', 'general')
    if not any(r['id'] == room_id for r in CHAT_ROOMS):
        room_id = 'general'
    current_room_name = next((r['name'] for r in CHAT_ROOMS if r['id'] == room_id), 'General')
    rows = ChatMessage.query.filter_by(room=room_id).order_by(ChatMessage.id.desc()).limit(50).all()
    rows.reverse()
    messages = []
    for m in rows:
        msg_data = {
            "id": m.id, "user_id": m.user_id, "user_name": m.user_name,
            "user_class": m.user_class,
            "user_avatar_color": m.user_avatar_color or '#00E1FD',
            "message": m.message, "time_ago": time_ago(m.created_at),
            "reply_to": None, "room": m.room
        }
        if m.reply_to_id:
            parent = ChatMessage.query.get(m.reply_to_id)
            if parent:
                msg_data["reply_to"] = {
                    "id": parent.id, "user_name": parent.user_name,
                    "message": parent.message
                }
        messages.append(msg_data)
    initial_msg_ids = [m['id'] for m in messages]
    online_users_list = get_live_online_users()
    online_users_data = [{"name": u['name'], "student_class": u['student_class'], "avatar_color": u.get('avatar_color', '#00E1FD')} for u in online_users_list]
    return render_template_string(
        CHAT_TEMPLATE,
        user=user, messages=messages, initial_msg_ids=initial_msg_ids,
        chat_rooms=CHAT_ROOMS, current_room=room_id,
        current_room_name=current_room_name,
        online_count=get_live_online_count(),
        room_online=get_room_online_count(room_id),
        total_users=get_total_user_count(),
        online_users=online_users_data,
        nav_html=bottom_nav_html("chat")
    )


@app.route('/analytics')
def analytics():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    touch_user(user); update_streak(user)
    stats = compute_stats(user.id)
    curriculum = get_full_curriculum(user.id)
    circ_small = 226.19
    ring_dash_small = round((stats['overall_pct'] / 100) * circ_small, 2)
    ring_gap_small = round(circ_small - ring_dash_small, 2)
    chapter_done_map = {}
    for subject, info in curriculum.items():
        chapter_done_map[subject] = {}
        for ci in range(len(info['chapters'])):
            done = ChapterProgress.query.filter_by(
                user_id=user.id, subject=subject, chapter_index=ci, completed=True
            ).count()
            chapter_done_map[subject][ci] = done
    week_data = get_7day_data(user.id)
    week_total = sum(d['count'] for d in week_data)
    week_avg = round(week_total / 7, 1)
    week_max = max((d['count'] for d in week_data), default=0)
    return render_template_string(
        ANALYTICS_TEMPLATE,
        user=user, stats=stats, curriculum=curriculum,
        ring_dash_small=ring_dash_small, ring_gap_small=ring_gap_small,
        chapter_done_map=chapter_done_map,
        week_data=week_data, week_total=week_total,
        week_avg=week_avg, week_max=week_max,
        online_count=get_live_online_count(),
        total_users=get_total_user_count(),
        nav_html=bottom_nav_html("analytics")
    )


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    touch_user(user)
    msg = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        student_class = request.form.get('student_class', '').strip()
        if name and student_class:
            user.name = name[:100]
            user.student_class = student_class[:50]
            try:
                db.session.commit()
                msg = "Profile updated successfully!"
            except Exception:
                db.session.rollback()
                msg = "Failed to update profile."
    stats = compute_stats(user.id)
    member_since = user.created_at.strftime('%b %d, %Y') if user.created_at else 'Recently'
    customs = CustomSubject.query.filter_by(user_id=user.id).order_by(CustomSubject.created_at).all()
    custom_subjects = []
    for cs in customs:
        count = CustomChapter.query.filter_by(subject_id=cs.id).count()
        custom_subjects.append({
            "id": cs.id, "name": cs.name, "icon": cs.icon,
            "chapter_count": count
        })
    return render_template_string(
        SETTINGS_TEMPLATE,
        user=user, overall_pct=stats['overall_pct'],
        msg=msg, member_since=member_since,
        custom_subjects=custom_subjects,
        avatar_colors=AVATAR_COLORS,
        nav_html=bottom_nav_html("settings")
    )


@app.route('/update_avatar', methods=['POST'])
def update_avatar():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    color = request.form.get('color', '#00E1FD')[:20]
    if color in AVATAR_COLORS:
        user.avatar_color = color
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return redirect(url_for('settings'))


@app.route('/reset', methods=['POST'])
def reset():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    ChapterProgress.query.filter_by(user_id=user.id).delete()
    ActivityLog.query.filter_by(user_id=user.id).delete()
    ExamSchedule.query.filter_by(user_id=user.id).delete()
    ChatMessage.query.filter_by(user_id=user.id).delete()
    customs = CustomSubject.query.filter_by(user_id=user.id).all()
    for cs in customs:
        CustomChapter.query.filter_by(subject_id=cs.id).delete()
        db.session.delete(cs)
    db.session.delete(user)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    session.clear()
    return redirect(url_for('onboarding'))


# ═════════════════════════════════════════════
# SOCKETIO REAL-TIME EVENTS (with app_context)
# ═════════════════════════════════════════════

def broadcast_presence():
    users = get_live_online_users()
    users_public = [{
        "name": u['name'],
        "student_class": u['student_class'],
        "avatar_color": u.get('avatar_color', '#00E1FD')
    } for u in users]
    socketio.emit('presence_update', {
        'online': len(users),
        'users': users_public
    })


@socketio.on('connect')
def on_connect():
    user_id = session.get('user_id')
    if not user_id:
        return False
    with app.app_context():
        user = User.query.get(user_id)
        if not user:
            return False
        active_sessions[request.sid] = {
            'user_id': user.id,
            'name': user.name,
            'student_class': user.student_class,
            'avatar_color': user.avatar_color or '#00E1FD',
            'room': None,
            'connected_at': datetime.utcnow()
        }
        try:
            user.last_seen = datetime.utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()
    broadcast_presence()


@socketio.on('disconnect')
def on_disconnect():
    info = active_sessions.pop(request.sid, None)
    if info and info.get('room'):
        room_name = next((r['name'] for r in CHAT_ROOMS if r['id'] == info['room']), info['room'])
        socketio.emit('user_left', {
            'user_id': info['user_id'],
            'name': info['name'],
            'room_name': room_name
        }, room=info['room'])
    broadcast_presence()


@socketio.on('join_chat_room')
def on_join_chat_room(data):
    sid = request.sid
    if sid not in active_sessions:
        return
    room = data.get('room', 'general')
    if not any(r['id'] == room for r in CHAT_ROOMS):
        room = 'general'
    info = active_sessions[sid]
    if info.get('room') and info['room'] != room:
        leave_room(info['room'])
    join_room(room)
    info['room'] = room
    room_name = next((r['name'] for r in CHAT_ROOMS if r['id'] == room), room)
    emit('user_joined', {
        'user_id': info['user_id'],
        'name': info['name'],
        'room_name': room_name
    }, room=room, include_self=False)
    users = get_live_online_users()
    users_public = [{
        "name": u['name'],
        "student_class": u['student_class'],
        "avatar_color": u.get('avatar_color', '#00E1FD')
    } for u in users]
    emit('presence_update', {
        'online': len(users),
        'room_online': get_room_online_count(room),
        'users': users_public
    })


@socketio.on('send_message')
def on_send_message(data):
    sid = request.sid
    info = active_sessions.get(sid)
    if not info:
        return
    user_id = info['user_id']
    room = data.get('room', info.get('room') or 'general')
    if not any(r['id'] == room for r in CHAT_ROOMS):
        return
    message = (data.get('message') or '').strip()
    if not message:
        return
    if len(message) > 1000:
        message = message[:1000]
    reply_to_id = data.get('reply_to_id')
    
    with app.app_context():
        user = User.query.get(user_id)
        if not user:
            return
        reply_to = None
        if reply_to_id:
            try:
                reply_to = int(reply_to_id)
                parent = ChatMessage.query.get(reply_to)
                if not parent or parent.room != room:
                    reply_to = None
            except (TypeError, ValueError):
                reply_to = None
        msg = ChatMessage(
            user_id=user.id, user_name=user.name,
            user_class=user.student_class,
            user_avatar_color=user.avatar_color or '#00E1FD',
            room=room, message=message, reply_to_id=reply_to
        )
        db.session.add(msg)
        try:
            user.last_seen = datetime.utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()
            return
        payload = {
            "id": msg.id, "user_id": msg.user_id, "user_name": msg.user_name,
            "user_class": msg.user_class,
            "user_avatar_color": msg.user_avatar_color,
            "message": msg.message, "time_ago": "just now",
            "room": msg.room, "reply_to": None
        }
        if reply_to:
            parent = ChatMessage.query.get(reply_to)
            if parent:
                payload["reply_to"] = {
                    "id": parent.id,
                    "user_name": parent.user_name,
                    "message": parent.message
                }
    socketio.emit('new_message', payload, room=room)


@socketio.on('typing')
def on_typing(data):
    sid = request.sid
    info = active_sessions.get(sid)
    if not info:
        return
    room = data.get('room', info.get('room') or 'general')
    emit('user_typing', {
        'user_id': info['user_id'],
        'name': info['name'],
        'room': room
    }, room=room, include_self=False)


# ═════════════════════════════════════════════
# DB INIT + AUTO MIGRATION (Works on SQLite + PostgreSQL)
# ═════════════════════════════════════════════

def init_db():
    """Safe DB initialization — never destroys data, works on both DBs."""
    with app.app_context():
        try:
            db.create_all()
            print("✅ Database tables ensured")
        except Exception as e:
            print(f"⚠️  create_all warning: {e}")

        from sqlalchemy import inspect, text
        try:
            inspector = inspect(db.engine)
            existing_tables = inspector.get_table_names()
        except Exception as e:
            print(f"⚠️  Could not inspect DB: {e}")
            return

        if 'user' not in existing_tables:
            print("ℹ️  Fresh database — no migrations needed")
            return

        try:
            existing_cols = [c['name'] for c in inspector.get_columns('user')]
        except Exception as e:
            print(f"⚠️  Could not read columns: {e}")
            return

        needed = {
            'created_at':   'TIMESTAMP',
            'last_active':  'DATE',
            'last_seen':    'TIMESTAMP',
            'streak':       'INTEGER DEFAULT 0',
            'avatar_color': "VARCHAR(20) DEFAULT '#00E1FD'"
        }

        migrations = []
        for col_name, col_type in needed.items():
            if col_name not in existing_cols:
                # Quote 'user' table name (reserved word in PostgreSQL)
                table_ref = '"user"' if IS_POSTGRES else 'user'
                migrations.append((col_name, f"ALTER TABLE {table_ref} ADD COLUMN {col_name} {col_type}"))

        if not migrations:
            print("✅ Schema up-to-date")
            return

        print(f"🔧 Running {len(migrations)} migration(s)...")
        for col_name, stmt in migrations:
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(stmt))
                print(f"   ✅ Added column: {col_name}")
            except Exception as e:
                print(f"   ⚠️  Skipped {col_name}: {e}")

        try:
            table_ref = '"user"' if IS_POSTGRES else 'user'
            with db.engine.begin() as conn:
                if IS_POSTGRES:
                    conn.execute(text(f"UPDATE {table_ref} SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
                    conn.execute(text(f"UPDATE {table_ref} SET last_active = CURRENT_DATE WHERE last_active IS NULL"))
                    conn.execute(text(f"UPDATE {table_ref} SET last_seen = CURRENT_TIMESTAMP WHERE last_seen IS NULL"))
                else:
                    conn.execute(text(f"UPDATE {table_ref} SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
                    conn.execute(text(f"UPDATE {table_ref} SET last_active = DATE('now') WHERE last_active IS NULL"))
                    conn.execute(text(f"UPDATE {table_ref} SET last_seen = CURRENT_TIMESTAMP WHERE last_seen IS NULL"))
                conn.execute(text(f"UPDATE {table_ref} SET streak = 0 WHERE streak IS NULL"))
                conn.execute(text(f"UPDATE {table_ref} SET avatar_color = '#00E1FD' WHERE avatar_color IS NULL"))
            print("✅ Backfill complete")
        except Exception as e:
            print(f"⚠️  Backfill warning: {e}")


# Initialize DB immediately when this module is imported (needed for Gunicorn on Render)
init_db()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = not IS_POSTGRES
    print(f"🚀 StudyTracker running on http://0.0.0.0:{port}")
    print("💡 Real-time chat enabled (async_mode: threading)")
    socketio.run(
        app,
        host='0.0.0.0',
        port=port,
        debug=debug_mode,
        allow_unsafe_werkzeug=True,
        use_reloader=False
    )
