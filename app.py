import os
import json
import threading
import time
from functools import lru_cache
from flask import Flask, request, session, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime, date, timedelta
from urllib.parse import quote as url_quote
from sqlalchemy import func
import math
import re

app = Flask(__name__)

# 
# SECRET KEY — Set SECRET_KEY in production; local .secret_key persists across restarts
# 
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
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('COOKIE_SECURE', '0') == '1'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=3650)  # approximately 10 years

# 
# DATABASE — PostgreSQL on Render, SQLite locally
# 
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
        'pool_size': 3,
        'max_overflow': 2,
        'pool_timeout': 10,
        'pool_use_lifo': True,
    }

print(f"  Database: {'PostgreSQL (Production)' if IS_POSTGRES else 'SQLite (Local)'}")

db = SQLAlchemy(app)
# Threading works with the Gunicorn sync worker + simple-websocket from the Procfile.
# Requesting eventlet here without eventlet installed makes production fail to boot.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


@lru_cache(maxsize=None)
def _compiled_template(source):
    # render_template_string recompiles the template on every request. These
    # templates are 20-30 KB each, so cache the compiled Jinja Template instead.
    return app.jinja_env.from_string(source)


def render_page(source, **context):
    app.update_template_context(context)
    return _compiled_template(source).render(context)

# 
# LIVE ONLINE TRACKING
# 

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


# 
# DATABASE MODELS
# 

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    student_class = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_active = db.Column(db.Date, default=date.today)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    streak = db.Column(db.Integer, default=0)
    avatar_color = db.Column(db.String(20), default='#A9DFFF')


class ChapterProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    subject = db.Column(db.String(100), nullable=False)
    chapter_index = db.Column(db.Integer, nullable=False)
    circle_index = db.Column(db.Integer, nullable=False)
    completed = db.Column(db.Boolean, default=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('user_id', 'subject', 'chapter_index', 'circle_index'),
    )


class CustomSubject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    icon = db.Column(db.String(10), default='')
    color_from = db.Column(db.String(20), default='#A9DFFF')
    color_to = db.Column(db.String(20), default='#75BFEA')
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
    user_avatar_color = db.Column(db.String(20), default='#A9DFFF')
    room = db.Column(db.String(50), default='general', index=True)
    message = db.Column(db.String(1000), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    reply_to_id = db.Column(db.Integer, db.ForeignKey('chat_message.id'), nullable=True)


# 
# CURRICULUM & META
# 

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


# Grade-aware CBSE curriculum (academic session 2026-27).
# Class IX was redesigned for 2026-27; Class X retains its own notified course structure.
GRADE_CURRICULA = {
    "9": {
        "Mathematics": [
            "Number Systems", "Polynomials", "Linear Equations in Two Variables",
            "Coordinate Geometry", "Introduction to Euclid’s Geometry", "Lines and Angles",
            "Triangles", "Quadrilaterals", "Circles", "Heron’s Formula",
            "Surface Areas and Volumes", "Statistics"
        ],
        "Science": [
            "Matter in Our Surroundings", "Is Matter Around Us Pure?", "Atoms and Molecules",
            "Structure of the Atom", "The Fundamental Unit of Life (Cell)", "Tissues",
            "Motion", "Force and Laws of Motion", "Gravitation", "Work and Energy",
            "Sound", "Improvement in Food Resources"
        ],
        "History": [
            "The French Revolution", "Socialism in Europe and the Russian Revolution",
            "Nazism and the Rise of Hitler", "Forest Society and Colonialism",
            "Pastoralists in the Modern World"
        ],
        "Geography": [
            "India – Size and Location", "Physical Features of India", "Drainage",
            "Climate", "Natural Vegetation and Wildlife", "Population"
        ],
        "Political Science": [
            "What is Democracy? Why Democracy?", "Constitutional Design", "Electoral Politics",
            "Working of Institutions", "Democratic Rights"
        ],
        "Economics": [
            "The Story of Village Palampur", "People as Resource", "Poverty as a Challenge",
            "Food Security in India"
        ],
        "English — Beehive (Prose)": [
            "The Fun They Had", "The Sound of Music", "The Little Girl", "A Truly Beautiful Mind",
            "The Snake and the Mirror", "My Childhood", "Reach for the Top", "Kathmandu", "If I Were You"
        ],
        "English — Beehive (Poems)": [
            "The Road Not Taken", "Wind", "Rain on the Roof", "The Lake Isle of Innisfree",
            "A Legend of the Northland", "No Men Are Foreign", "On Killing a Tree",
            "A Slumber Did My Spirit Seal"
        ],
        "English — Moments": [
            "The Lost Child", "The Adventures of Toto", "Iswaran the Storyteller",
            "In the Kingdom of Fools", "The Happy Prince", "Weathering the Storm in Ersama",
            "The Last Leaf", "A House Is Not a Home", "The Beggar"
        ]
    },
    "10": {
        "Mathematics": [
            "Real Numbers", "Polynomials", "Pair of Linear Equations in Two Variables",
            "Quadratic Equations", "Arithmetic Progressions", "Coordinate Geometry",
            "Triangles", "Circles", "Introduction to Trigonometry",
            "Some Applications of Trigonometry", "Area Related to Circles",
            "Surface Areas and Volumes", "Statistics", "Probability"
        ],
        "Science": [
            "Chemical Reactions and Equations", "Acids, Bases and Salts", "Metals and Non-metals",
            "Carbon and its Compounds", "Life Processes", "Control and Coordination",
            "How do Organisms Reproduce?", "Heredity", "Light – Reflection and Refraction",
            "The Human Eye and the Colorful World", "Electricity",
            "Magnetic Effects of Electric Current", "Our Environment"
        ],
        "History": [
            "The Rise of Nationalism in Europe", "Nationalism in India", "The Making of a Global World",
            "The Age of Industrialisation", "Print Culture and the Modern World"
        ],
        "Geography": [
            "Resources and Development", "Forest and Wildlife Resources", "Water Resources",
            "Agriculture", "Minerals and Energy Resources", "Manufacturing Industries",
            "Lifelines of National Economy"
        ],
        "Political Science": [
            "Power Sharing", "Federalism", "Gender, Religion and Caste", "Political Parties",
            "Outcomes of Democracy"
        ],
        "Economics": [
            "Development", "Sectors of the Indian Economy", "Money and Credit",
            "Globalisation and the Indian Economy", "Consumer Rights (Project Work / Evaluation)"
        ],
        "English — First Flight (Prose)": [
            "A Letter to God", "Nelson Mandela: Long Walk to Freedom", "Two Stories about Flying",
            "From the Diary of Anne Frank", "Glimpses of India", "Mijbil the Otter",
            "Madam Rides the Bus", "The Sermon at Benares", "The Proposal"
        ],
        "English — First Flight (Poems)": [
            "Dust of Snow", "Fire and Ice", "A Tiger in the Zoo", "How to Tell Wild Animals",
            "The Ball Poem", "Amanda!", "The Trees", "Fog", "The Tale of Custard the Dragon",
            "For Anne Gregory"
        ],
        "English — Footprints Without Feet": [
            "A Triumph of Surgery", "The Thief’s Story", "The Midnight Visitor", "A Question of Trust",
            "Footprints without Feet", "The Making of a Scientist", "The Necklace", "Bholi",
            "The Book That Saved the Earth"
        ]
    }
}

GRADE_SUBJECT_META = {
    "Mathematics": {"icon":"M","color_from":"#6D9BFF","color_to":"#4D55D9","tag":"NUMBER · ALGEBRA · GEOMETRY"},
    "Science": {"icon":"S","color_from":"#9B7BFF","color_to":"#5B4BDB","tag":"PHYSICS · CHEMISTRY · BIOLOGY"},
    "History": {"icon":"H","color_from":"#B18CFF","color_to":"#7046D8","tag":"EVENTS · PEOPLE · CHANGE"},
    "Geography": {"icon":"G","color_from":"#55B9C8","color_to":"#367EAB","tag":"EARTH · RESOURCES · INDIA"},
    "Political Science": {"icon":"P","color_from":"#E0A05B","color_to":"#A85C42","tag":"DEMOCRACY · RIGHTS · GOVERNMENT"},
    "Economics": {"icon":"E","color_from":"#62B99A","color_to":"#367B73","tag":"DEVELOPMENT · MARKETS · FINANCE"},
    "English — Beehive (Prose)": {"icon":"B","color_from":"#FF9CBA","color_to":"#B35CE6","tag":"BEEHIVE · PROSE"},
    "English — Beehive (Poems)": {"icon":"P","color_from":"#D28BFF","color_to":"#7956C9","tag":"BEEHIVE · POETRY"},
    "English — Moments": {"icon":"M","color_from":"#E0A0C0","color_to":"#9D5B91","tag":"MOMENTS · SUPPLEMENTARY READER"},
    "English — First Flight (Prose)": {"icon":"F","color_from":"#FF9CBA","color_to":"#B35CE6","tag":"FIRST FLIGHT · PROSE"},
    "English — First Flight (Poems)": {"icon":"P","color_from":"#D28BFF","color_to":"#7956C9","tag":"FIRST FLIGHT · POETRY"},
    "English — Footprints Without Feet": {"icon":"F","color_from":"#E0A0C0","color_to":"#9D5B91","tag":"SUPPLEMENTARY READER"}
}

CBSE_SYLLABUS_SESSION = "2026-27"
CBSE_SYLLABUS_URL = "https://cbseacademic.nic.in/curriculum_2027.html"


def get_student_grade(user_id):
    """Resolve a student's selected grade from existing free-text profile data."""
    user = db.session.get(User, user_id)
    raw = (user.student_class if user else "10").lower()
    if re.search(r"(^|\D)(9|ix)(\D|$)", raw):
        return "9"
    return "10"


def get_grade_curriculum(grade):
    grade = "9" if str(grade) in ("9", "IX", "ix") else "10"
    return {
        name: {
            "chapters": chapters,
            "meta": GRADE_SUBJECT_META[name],
            "is_custom": False,
            "subject_id": None
        }
        for name, chapters in GRADE_CURRICULA[grade].items()
    }


# Chapter-specific learning content. These are original revision notes, not copied textbook passages.
# Where a chapter has a multi-part literary lesson, its key parts are included in the notes/mind map.
CHAPTER_FACTS = {
    "Number Systems": ("Rational numbers have terminating or recurring decimal expansions; irrational numbers have non-terminating, non-recurring expansions. Real numbers combine both sets.", ["Classify numbers as natural, whole, integer, rational, irrational or real.", "Use prime factorisation and the Fundamental Theorem of Arithmetic.", "Locate irrational numbers on the number line and simplify surds.", "For a rational number p/q in lowest form, its decimal terminates exactly when q has no prime factors other than 2 and 5."], ["Real numbers", "Rational / irrational", "Prime factorisation", "Decimal expansion", "Number line"]),
    "Polynomials": ("A polynomial is an algebraic expression with non-negative integer powers of its variable. Its zeroes are values that make the polynomial equal to zero.", ["Identify terms, coefficients, degree and constant term.", "Find zeroes by factorisation or inspection.", "For a quadratic ax²+bx+c, sum of zeroes = −b/a and product = c/a.", "Relate the graph of a polynomial to its zeroes: zeroes are x-intercepts."], ["Polynomial", "Terms & degree", "Zeroes", "Factorisation", "Zeroes–coefficients relation"]),
    "Pair of Linear Equations in Two Variables": ("A pair of linear equations represents two straight lines. Their intersection determines whether the system has one, no or infinitely many solutions.", ["Solve by graphical, substitution, elimination and cross-multiplication methods.", "Unique solution: a₁/a₂ ≠ b₁/b₂; no solution: a₁/a₂ = b₁/b₂ ≠ c₁/c₂.", "Infinitely many solutions: a₁/a₂ = b₁/b₂ = c₁/c₂.", "Convert word problems into two variables and two linear equations."], ["Two equations", "Graphical method", "Substitution", "Elimination", "Consistency of solutions", "Word problems"]),
    "Linear Equations in Two Variables": ("A linear equation in two variables has the form ax + by + c = 0. Its solutions form a straight line on the Cartesian plane.", ["An ordered pair (x,y) is a solution when it satisfies the equation.", "A linear equation in two variables generally has infinitely many solutions.", "Plot at least two solution points to draw its graph.", "The x- and y-intercepts are found by setting the other variable to zero."], ["ax + by + c = 0", "Ordered pairs", "Solution table", "Graph", "Intercepts"]),
    "Quadratic Equations": ("A quadratic equation has the standard form ax²+bx+c=0, where a is non-zero. Its roots are the values that satisfy the equation.", ["Solve by factorisation, completing the square or the quadratic formula.", "Discriminant D=b²−4ac determines the nature of roots.", "D>0: two distinct real roots; D=0: equal real roots; D<0: no real roots.", "Check roots by substituting them into the original equation."], ["Standard form", "Factorisation", "Quadratic formula", "Discriminant", "Nature of roots", "Applications"]),
    "Arithmetic Progressions": ("An arithmetic progression (AP) is a sequence in which the difference between consecutive terms is constant.", ["Common difference d = a₂−a₁.", "nth term: aₙ = a + (n−1)d.", "Sum of first n terms: Sₙ = n/2[2a+(n−1)d] = n/2(a+l).", "Identify the first term, common difference and number of terms before substituting."], ["Sequence", "First term a", "Common difference d", "nth term", "Sum Sₙ", "Applications"]),
    "Coordinate Geometry": ("Coordinate geometry describes points and shapes using ordered pairs on perpendicular number lines called axes.", ["The horizontal axis is x and the vertical axis is y; their intersection is the origin (0,0).", "Read coordinates in the order (x,y).", "The signs of x and y determine the quadrant.", "Distance formula: √[(x₂−x₁)²+(y₂−y₁)²]; midpoint: ((x₁+x₂)/2,(y₁+y₂)/2)."], ["Cartesian plane", "Axes & origin", "Ordered pair", "Quadrants", "Plotting points", "Distance / midpoint"]),
    "Triangles": ("Triangles are classified by sides and angles. Congruence and similarity theorems establish when two triangles have matching shape or size.", ["Congruence criteria include SSS, SAS, ASA/AAS and RHS.", "In similar triangles, corresponding angles are equal and corresponding sides are proportional.", "The Basic Proportionality Theorem links a line parallel to one side with proportional division of the other two sides.", "Use a labelled diagram and state the theorem before proving a result."], ["Triangle types", "Congruence", "Similarity", "BPT", "Pythagoras theorem", "Proofs & applications"]),
    "Circles": ("A circle is the set of points at a fixed distance from a centre. Chord, radius, diameter and tangent are central ideas.", ["Equal chords subtend equal angles at the centre and are equidistant from the centre.", "The perpendicular from the centre to a chord bisects the chord.", "The tangent at any point of a circle is perpendicular to the radius through the point of contact.", "Tangents drawn from an external point to a circle are equal in length."], ["Centre & radius", "Chord & diameter", "Angle properties", "Perpendicular from centre", "Tangent", "Theorems"]),
    "Introduction to Trigonometry": ("Trigonometric ratios connect an acute angle of a right triangle with ratios of its sides.", ["sin θ = opposite/hypotenuse; cos θ = adjacent/hypotenuse; tan θ = opposite/adjacent.", "Reciprocal ratios: cosec θ, sec θ and cot θ.", "Identities: sin²θ+cos²θ=1; 1+tan²θ=sec²θ; 1+cot²θ=cosec²θ.", "Learn standard values for 0°, 30°, 45°, 60° and 90°."], ["Right triangle", "Opposite / adjacent / hypotenuse", "Six ratios", "Standard values", "Identities", "Applications"]),
    "Some Applications of Trigonometry": ("Heights and distances problems use angles of elevation or depression with right-triangle trigonometry.", ["Draw a clear vertical-horizontal diagram before choosing a ratio.", "Angle of elevation is measured upward from the horizontal; depression is measured downward.", "Use tan θ = height/base when opposite and adjacent sides are involved.", "Keep units consistent and state the final height or distance with units."], ["Line of sight", "Horizontal line", "Elevation", "Depression", "Right triangle", "Choose ratio & solve"]),
    "Heron’s Formula": ("Heron’s formula finds the area of a triangle when all three side lengths are known.", ["Semi-perimeter s=(a+b+c)/2.", "Area = √[s(s−a)(s−b)(s−c)].", "All side lengths must use the same unit.", "For a quadrilateral, divide it into triangles and add their areas when suitable."], ["Three sides", "Semi-perimeter", "Heron’s formula", "Substitution", "Area units", "Applications"]),
    "Surface Areas and Volumes": ("Mensuration studies the surface area and volume of three-dimensional solids.", ["Cuboid: V=lbh; TSA=2(lb+bh+hl). Cube: V=a³; TSA=6a².", "Cylinder: V=πr²h; CSA=2πrh; TSA=2πr(r+h).", "Cone: V=⅓πr²h; slant height l=√(r²+h²); CSA=πrl.", "Sphere: V=4/3πr³; surface area=4πr². Hemisphere: V=2/3πr³.", "For combined solids, add or subtract volumes and exposed surface areas carefully."], ["3-D solids", "Dimensions", "Curved surface area", "Total surface area", "Volume", "Combined solids"]),
    "Statistics": ("Statistics organises data and describes its centre using measures such as mean, median and mode.", ["Arrange raw data into a frequency distribution when useful.", "Mean for grouped data: Σfᵢxᵢ/Σfᵢ; xᵢ is the class mark.", "Median is the middle observation; for grouped data use the median-class formula.", "Mode is the most frequent value; identify the modal class for grouped data.", "Choose a suitable scale and label both axes in graphs."], ["Collect data", "Frequency table", "Class mark", "Mean", "Median", "Mode", "Graphs"]),
    "Probability": ("Theoretical probability measures the chance of an event when outcomes are equally likely.", ["P(E)=number of favourable outcomes / total number of equally likely outcomes.", "0≤P(E)≤1; an impossible event has probability 0 and a sure event has probability 1.", "P(not E)=1−P(E).", "List the sample space carefully to avoid missing or repeating outcomes."], ["Random experiment", "Sample space", "Event", "Favourable outcomes", "Probability", "Complement"]),
    "Matter in Our Surroundings": ("Matter has mass and occupies space. Its particles are continuously moving and have spaces and forces between them.", ["Diffusion is the intermixing of particles due to their motion.", "Temperature and pressure can change the state of matter.", "Melting, freezing, vaporisation, condensation and sublimation are changes of state.", "Evaporation causes cooling and depends on surface area, temperature, humidity and wind speed."], ["Matter", "Particles", "States", "Change of state", "Latent heat", "Evaporation"]),
    "Is Matter Around Us Pure?": ("A pure substance contains one kind of particle; mixtures contain two or more substances physically combined.", ["Solutions are homogeneous; suspensions and many colloids are heterogeneous.", "Separate mixtures using filtration, evaporation, centrifugation, chromatography or distillation as appropriate.", "A solution has solute and solvent; concentration describes the amount of solute.", "Elements and compounds are pure substances; mixtures can have variable composition."], ["Pure substances", "Mixtures", "Solutions", "Suspensions", "Colloids", "Separation methods"]),
    "Atoms and Molecules": ("Atoms are the basic units of elements and molecules are groups of atoms chemically bonded together.", ["Law of conservation of mass: mass is neither created nor destroyed in a chemical reaction.", "Law of constant proportions: a compound has elements in a fixed proportion by mass.", "Use chemical symbols, valency and formulae to represent compounds.", "Molecular mass is the sum of the atomic masses of all atoms in a molecule."], ["Atoms", "Molecules", "Chemical laws", "Symbols & valency", "Chemical formulae", "Molecular mass"]),
    "Structure of the Atom": ("Atoms contain a positively charged nucleus with protons and neutrons, surrounded by electrons.", ["Atomic number Z equals the number of protons; in a neutral atom it also equals electrons.", "Mass number A = protons + neutrons.", "Isotopes have the same atomic number but different mass numbers; isobars have the same mass number.", "Electrons occupy shells; valence electrons influence chemical properties."], ["Subatomic particles", "Nucleus", "Atomic number", "Mass number", "Electron shells", "Isotopes & isobars"]),
    "The Fundamental Unit of Life (Cell)": ("The cell is the basic structural and functional unit of life. Cell organelles perform specialised functions.", ["The plasma membrane controls movement of substances into and out of the cell.", "The nucleus contains genetic material and helps control cell activities.", "Mitochondria release energy; ribosomes make proteins; vacuoles store materials.", "Plant cells have a cell wall and plastids; distinguish them from animal cells."], ["Cell theory", "Cell membrane", "Nucleus", "Cytoplasm", "Organelles", "Plant vs animal cell"]),
    "Tissues": ("A tissue is a group of similar cells that work together to perform a specific function.", ["Plant tissues include meristematic and permanent tissues.", "Animal tissues include epithelial, connective, muscular and nervous tissues.", "Xylem transports water and minerals; phloem transports food in plants.", "Structure is related to function: compare location, cell shape and role."], ["Tissue", "Plant tissues", "Meristematic", "Permanent", "Animal tissues", "Transport & coordination"]),
    "Motion": ("Motion describes change in position with time and is measured using distance, displacement, speed, velocity and acceleration.", ["Speed=distance/time; velocity=displacement/time.", "Acceleration=(final velocity−initial velocity)/time.", "For uniform acceleration: v=u+at; s=ut+½at²; v²=u²+2as.", "Slope of a distance-time graph gives speed; slope of a velocity-time graph gives acceleration."], ["Reference point", "Distance & displacement", "Speed & velocity", "Acceleration", "Equations of motion", "Graphs"]),
    "Force and Laws of Motion": ("A force can change an object's state of motion or shape. Newton’s laws describe the relationship between force and motion.", ["First law: an object maintains its state unless acted on by an unbalanced external force.", "Second law: force is the rate of change of momentum; for constant mass F=ma.", "Third law: every action has an equal and opposite reaction acting on a different body.", "Momentum p=mv; SI unit is kg m/s. Impulse changes momentum."], ["Force", "Inertia", "Newton's first law", "F=ma", "Action–reaction", "Momentum"]),
    "Gravitation": ("Every mass attracts every other mass. Earth's gravity gives objects weight and governs falling motion.", ["Universal law: F=Gm₁m₂/r².", "Weight W=mg; mass is constant while weight depends on gravitational field strength.", "Free fall occurs when gravity is the only force considered.", "Pressure=thrust/area; buoyant force is the upward force exerted by a fluid."], ["Universal gravitation", "Free fall", "g", "Mass & weight", "Pressure", "Buoyancy"]),
    "Work and Energy": ("Work is done when a force causes displacement. Energy is the capacity to do work.", ["Work W=F×s in the direction of force; SI unit joule.", "Kinetic energy=½mv²; gravitational potential energy=mgh.", "Power=work/time; SI unit watt.", "Energy can change form but total energy is conserved in an isolated system."], ["Work", "Energy", "Kinetic energy", "Potential energy", "Conservation", "Power"]),
    "Sound": ("Sound is produced by vibrating objects and travels through a material medium as a mechanical wave.", ["Sound cannot travel through vacuum.", "Frequency determines pitch; amplitude relates to loudness.", "Speed=frequency×wavelength.", "The human audible range is approximately 20 Hz to 20,000 Hz; echoes result from reflection."], ["Vibration", "Medium", "Longitudinal wave", "Frequency", "Amplitude", "Echo & ultrasound"]),
    "Improvement in Food Resources": ("Food security and higher agricultural productivity depend on crop variety improvement, crop production management and crop protection.", ["Crop variety improvement targets yield, quality, resistance and maturity duration.", "Nutrient management uses manure and fertilisers; irrigation supplies water.", "Mixed cropping, intercropping and crop rotation help manage risk and soil fertility.", "Animal husbandry includes cattle, poultry, fisheries and beekeeping."], ["Crop variety", "Nutrient management", "Irrigation", "Cropping patterns", "Crop protection", "Animal husbandry"]),
    "Chemical Reactions and Equations": ("A chemical reaction rearranges atoms to form new substances. A balanced equation has equal numbers of each type of atom on both sides.", ["Observe colour change, gas evolution, precipitate formation or temperature change as possible indicators.", "Balance equations using coefficients; never change chemical formula subscripts.", "Reaction types include combination, decomposition, displacement and double displacement.", "Oxidation involves gain of oxygen or loss of hydrogen; reduction is the reverse in common school examples."], ["Reactants → products", "Observations", "Balancing", "Combination", "Decomposition", "Displacement", "Redox"]),
    "Acids, Bases and Salts": ("Acids and bases show characteristic behaviour in water and can be identified using indicators and pH.", ["Acids produce H⁺/H₃O⁺ ions in aqueous solution; bases produce OH⁻ ions.", "pH below 7 is acidic, 7 is neutral and above 7 is basic at room temperature.", "Neutralisation: acid + base → salt + water.", "Study common salts such as baking soda, washing soda, bleaching powder and plaster of Paris."], ["Acids", "Bases", "Indicators", "pH scale", "Neutralisation", "Common salts"]),
    "Metals and Non-metals": ("Metals and non-metals differ in physical and chemical properties; their reactivity determines how they occur and are extracted.", ["Metals are generally malleable, ductile and good conductors; exceptions exist.", "Metals react with oxygen, water and acids according to their reactivity.", "The reactivity series predicts displacement reactions.", "Ionic compounds form by electron transfer and usually have high melting points."], ["Physical properties", "Chemical reactions", "Reactivity series", "Ionic compounds", "Corrosion", "Extraction"]),
    "Carbon and its Compounds": ("Carbon forms covalent bonds and a large variety of compounds because of tetravalency and catenation.", ["Covalent bonds form by sharing electron pairs.", "Saturated hydrocarbons have single bonds; unsaturated hydrocarbons have double or triple bonds.", "Learn homologous series, functional groups and basic nomenclature.", "Ethanol and ethanoic acid are important carbon compounds; soaps and detergents clean by different mechanisms."], ["Tetravalency", "Catenation", "Covalent bonding", "Hydrocarbons", "Functional groups", "Ethanol / ethanoic acid", "Soaps"]),
    "Life Processes": ("Living organisms maintain life through nutrition, respiration, transportation and excretion.", ["Autotrophic nutrition uses light energy; heterotrophs obtain food from other organisms.", "Aerobic respiration releases more energy than anaerobic respiration.", "Human transport involves the heart, blood and blood vessels; plants use xylem and phloem.", "Kidneys filter blood and form urine; plants remove wastes through several routes."], ["Nutrition", "Respiration", "Transportation", "Excretion", "Human systems", "Plant systems"]),
    "Control and Coordination": ("Nervous and hormonal systems coordinate responses in animals; plant hormones coordinate growth and responses.", ["A neuron carries electrical impulses; reflex actions provide rapid responses.", "The brain and spinal cord form the central nervous system.", "Hormones are chemical messengers released by endocrine glands.", "Plant responses include tropisms; auxin, gibberellin, cytokinin and abscisic acid have distinct roles."], ["Stimulus", "Neuron", "Reflex arc", "Brain & spinal cord", "Endocrine glands", "Plant hormones"]),
    "How do Organisms Reproduce?": ("Reproduction produces new individuals and transfers hereditary information from one generation to the next.", ["Asexual reproduction involves one parent and methods such as fission, budding, fragmentation and spore formation.", "Sexual reproduction involves formation and fusion of gametes.", "In flowering plants, pollination precedes fertilisation and seed formation.", "Human reproductive systems produce gametes; reproductive health includes responsible health practices."], ["Asexual reproduction", "Sexual reproduction", "Gametes", "Pollination", "Fertilisation", "Human reproduction"]),
    "Heredity": ("Heredity is the transmission of traits from parents to offspring through genes.", ["Genes are segments of DNA located on chromosomes.", "Dominant and recessive alleles help explain inherited traits.", "Mendel's monohybrid cross illustrates segregation of alleles.", "Inherited variation contributes to evolution; acquired traits are not generally passed through genes."], ["DNA", "Chromosomes", "Genes", "Alleles", "Mendelian inheritance", "Variation"]),
    "Light – Reflection and Refraction": ("Light changes direction by reflection and refraction. Mirrors and lenses form images according to geometrical rules.", ["Law of reflection: angle of incidence equals angle of reflection.", "Use ray diagrams to locate images formed by spherical mirrors and lenses.", "Mirror formula: 1/f=1/v+1/u; magnification for mirrors m=−v/u.", "Lens formula: 1/f=1/v−1/u; power P=1/f (f in metres), measured in dioptres."], ["Reflection", "Spherical mirrors", "Ray diagrams", "Refraction", "Lenses", "Formulae & sign convention"]),
    "The Human Eye and the Colorful World": ("The eye focuses light on the retina. Defects of vision and atmospheric optical effects can be explained using refraction and scattering.", ["Accommodation changes the focal length of the eye lens.", "Myopia is corrected using a concave lens; hypermetropia using a convex lens.", "Presbyopia is age-related reduced accommodation.", "Dispersion splits white light into colours; scattering explains the blue sky and reddish sunrise/sunset."], ["Eye structure", "Accommodation", "Myopia", "Hypermetropia", "Dispersion", "Scattering"]),
    "Electricity": ("Electric current is the rate of flow of charge. Potential difference drives current through a circuit.", ["I=Q/t; potential difference V=W/Q.", "Ohm's law: V=IR for an ohmic conductor at constant physical conditions.", "Series resistances add; reciprocal resistances add in parallel.", "Power P=VI=I²R=V²/R; electrical energy is measured in joules or kilowatt-hours."], ["Charge & current", "Potential difference", "Ohm's law", "Resistance", "Series / parallel", "Power & energy"]),
    "Magnetic Effects of Electric Current": ("An electric current produces a magnetic field. Electromagnets and electric motors use the interaction of current and magnetic fields.", ["Magnetic field lines form closed curves and never intersect.", "Use the right-hand thumb rule to find the field direction around a straight conductor.", "A current-carrying conductor in a magnetic field experiences a force; Fleming's left-hand rule gives direction.", "Electromagnetic induction produces current when magnetic flux changes; Fleming's right-hand rule gives induced-current direction."], ["Magnetic field", "Field lines", "Right-hand thumb rule", "Motor effect", "Electromagnetic induction", "Motor / generator"]),
    "Our Environment": ("An ecosystem includes living organisms and their physical environment, linked by energy flow and nutrient cycles.", ["Producers capture energy; consumers feed at successive trophic levels; decomposers recycle nutrients.", "Only a small fraction of energy passes to the next trophic level (10% law as a school-level model).", "Food chains connect into food webs.", "Biodegradable and non-biodegradable wastes have different environmental impacts; ozone protects life from harmful UV radiation."], ["Ecosystem", "Producers", "Consumers", "Decomposers", "Food chain / web", "Waste & ozone"]),
    "The French Revolution": ("The French Revolution challenged absolute monarchy and privilege and spread ideas of citizenship, equality and popular sovereignty.", ["The Old Regime had estates with unequal privileges and tax burdens.", "Financial crisis, food shortages and social inequality intensified unrest in 1789.", "The National Assembly, the fall of the Bastille and the Declaration of Rights marked key turning points.", "Revolutionary France moved through monarchy, republic, Terror and the rise of Napoleon."], ["Old Regime", "Three Estates", "1789", "National Assembly", "Rights of Man", "Republic & Napoleon"]),
    "Socialism in Europe and the Russian Revolution": ("Socialist ideas criticised inequality under capitalism. The Russian Revolution of 1917 ended Tsarist rule and brought the Bolsheviks to power.", ["Liberals, radicals and conservatives differed over political and social change.", "Industrialisation created new working-class conditions and socialist movements.", "The 1905 Revolution and World War I weakened the Tsarist regime.", "The February and October Revolutions of 1917 led to provisional government collapse and Bolshevik rule."], ["Political ideologies", "Industrial society", "1905", "World War I", "February 1917", "October 1917"]),
    "Nazism and the Rise of Hitler": ("Nazism grew amid Germany's post-war crisis, economic hardship and political instability, leading to dictatorship and genocide.", ["The Treaty of Versailles and the Weimar Republic faced legitimacy and economic challenges.", "The Great Depression increased unemployment and political extremism.", "Hitler used propaganda, repression and discriminatory laws to consolidate dictatorship.", "Nazi racial ideology led to persecution, the Holocaust and mass violence."], ["Weimar Republic", "Versailles", "Depression", "Hitler's rise", "Nazi state", "Holocaust"]),
    "Nationalism in India": ("Indian nationalism expanded through mass movements against colonial rule, drawing on diverse groups and methods of resistance.", ["The Rowlatt Act and Jallianwala Bagh intensified anti-colonial anger.", "Gandhi launched the Non-Cooperation and Civil Disobedience movements.", "The Salt March made colonial laws a focus of mass protest.", "Peasants, workers, business groups, women and different communities participated in varied ways."], ["Colonial rule", "Gandhian methods", "Non-Cooperation", "Salt March", "Civil Disobedience", "Mass participation"]),
    "Resources and Development": ("Resources are useful materials and capabilities that become resources through human needs, technology and institutions.", ["Classify resources by origin, exhaustibility, ownership and status of development.", "Resource planning includes identification, planning structure and matching resources with development plans.", "Sustainable development balances present needs with future generations.", "Soil formation, erosion and conservation practices are key topics."], ["Meaning of resource", "Classification", "Planning", "Sustainability", "Soil types", "Conservation"]),
    "Power Sharing": ("Power sharing distributes authority among institutions, levels and social groups to strengthen democracy and reduce conflict.", ["Belgium and Sri Lanka illustrate different approaches to diversity and power.", "Horizontal sharing separates legislature, executive and judiciary.", "Vertical sharing distributes power among central, state and local governments.", "Power may also be shared among social groups, parties and pressure groups."], ["Why share power?", "Belgium", "Sri Lanka", "Horizontal", "Vertical", "Social groups & parties"]),
    "Federalism": ("Federalism is a system in which power is constitutionally divided between more than one level of government.", ["Key features include multiple levels, constitutional division of powers and independent courts.", "India has Union, State and local government levels.", "Legislative subjects are divided through Union, State and Concurrent Lists.", "Decentralisation strengthens local participation through panchayats and municipalities."], ["Federal system", "Constitution", "Union / State lists", "India's federalism", "Language policy", "Decentralisation"]),
    "Development": ("Development has different meanings for different people and includes income as well as health, education, security and dignity.", ["Per capita income is average income, not a complete measure of well-being.", "Compare development using indicators such as literacy, life expectancy and infant mortality.", "Public facilities and equitable access influence quality of life.", "Sustainable development protects resources and opportunities for future generations."], ["Development goals", "Income", "Per capita income", "Human development", "Public facilities", "Sustainability"]),
    "Money and Credit": ("Money acts as a medium of exchange and makes transactions easier. Credit can support activity but may also create debt traps.", ["Modern forms of money include currency and bank deposits.", "Banks accept deposits and provide loans; the difference between lending and deposit rates supports their activity.", "Formal credit is regulated; informal credit may carry high interest.", "Self-help groups can improve access to small loans, especially for people with limited collateral."], ["Barter limits", "Money functions", "Deposits", "Banks & loans", "Formal / informal credit", "Self-help groups"]),
    "The Rise of Nationalism in Europe": ("Nationalism reshaped Europe through revolutions, cultural movements and the unification of territories into nation-states.", ["The French Revolution introduced ideas of citizenship and nationhood.", "Liberal nationalism promoted constitutionalism and equality before law, though political rights were limited.", "The unifications of Germany and Italy involved political leadership, diplomacy and war.", "Balkan nationalism contributed to tensions in Europe."], ["French Revolution", "Liberal nationalism", "Revolutions", "Germany", "Italy", "Balkans"]),
    "The Making of a Global World": ("Globalisation has historical roots in trade, migration, conquest, labour movement and the exchange of ideas and goods.", ["Pre-modern trade routes connected distant regions through goods and cultures.", "Colonialism and disease transformed societies and economies.", "Indentured labour moved workers across the world.", "The inter-war economy, Great Depression and post-war institutions reshaped global connections."], ["Early connections", "Trade routes", "Conquest & disease", "Labour migration", "Great Depression", "Post-war world"]),
    "The Age of Industrialisation": ("Industrialisation transformed production, labour, markets and urban life, but hand production and small workshops continued alongside factories.", ["Proto-industrialisation preceded factory production.", "Machines and factories changed production, but adoption varied across industries.", "Workers faced uncertain employment, long hours and difficult conditions.", "In India, industrial growth interacted with colonial trade, handloom production and new entrepreneurs."], ["Proto-industrialisation", "Factories", "Machines", "Workers", "Indian industries", "Markets & advertisements"]),
    "Print Culture and the Modern World": ("Print technology transformed the circulation of information, religious debate, public opinion and political movements.", ["The printing press spread from East Asia to Europe; Gutenberg's press accelerated book production.", "Print encouraged religious debates, scientific ideas and wider reading publics.", "Print culture supported reform movements and nationalist politics in India.", "Colonial authorities used censorship while readers and writers developed new public spaces."], ["Early print", "Printing press", "Reading public", "Reformation", "Print in India", "Censorship"]),
    "The Story of Village Palampur": ("Palampur is a fictional village used to explain production, factors of production and farming practices.", ["The four factors of production are land, labour, physical capital and human capital.", "Multiple cropping and modern farming methods can raise output but require inputs.", "Small farmers often borrow; large farmers may generate surplus for sale.", "Non-farm activities include dairy, small-scale manufacturing, shopkeeping and transport."], ["Production", "Four factors", "Farming", "Modern inputs", "Land distribution", "Non-farm work"]),
    "People as Resource": ("People become a resource when investment in education, health and skills improves their productive capabilities.", ["Human capital formation increases productivity and future earning potential.", "Education and health are important investments in people.", "Economic activities are market and non-market activities.", "Unemployment includes seasonal and disguised forms in rural areas and educated unemployment in urban areas."], ["Population as asset", "Human capital", "Education", "Health", "Economic activities", "Unemployment"]),
    "Food Security in India": ("Food security means that all people have access to sufficient, safe and nutritious food at all times.", ["Availability, accessibility and affordability are the three dimensions of food security.", "The Public Distribution System distributes food grains through fair-price shops.", "Buffer stocks are maintained by public agencies to manage shortages and price stability.", "Antyodaya Anna Yojana and other schemes target vulnerable households."], ["Food security", "Availability", "Accessibility", "Affordability", "Buffer stock", "PDS & schemes"]),
}

HINDI_TITLES = {
    "Mathematics":"गणित", "Science":"विज्ञान", "History":"इतिहास", "Geography":"भूगोल",
    "Political Science":"राजनीति विज्ञान", "Economics":"अर्थशास्त्र",
    "Number Systems":"संख्या पद्धति", "Polynomials":"बहुपद", "Linear Equations in Two Variables":"दो चरों वाले रैखिक समीकरण",
    "Pair of Linear Equations in Two Variables":"दो चरों वाले रैखिक समीकरणों का युग्म", "Coordinate Geometry":"निर्देशांक ज्यामिति",
    "Introduction to Euclid’s Geometry":"यूक्लिड की ज्यामिति का परिचय", "Lines and Angles":"रेखाएँ और कोण",
    "Triangles":"त्रिभुज", "Quadrilaterals":"चतुर्भुज", "Circles":"वृत्त", "Heron’s Formula":"हीरोन का सूत्र",
    "Surface Areas and Volumes":"पृष्ठीय क्षेत्रफल और आयतन", "Statistics":"सांख्यिकी", "Probability":"प्रायिकता",
    "Matter in Our Surroundings":"हमारे आसपास के पदार्थ", "Is Matter Around Us Pure?":"क्या हमारे आसपास के पदार्थ शुद्ध हैं?",
    "Atoms and Molecules":"परमाणु एवं अणु", "Structure of the Atom":"परमाणु की संरचना", "The Fundamental Unit of Life (Cell)":"जीवन की मूल इकाई (कोशिका)",
    "Tissues":"ऊतक", "Motion":"गति", "Force and Laws of Motion":"बल तथा गति के नियम", "Gravitation":"गुरुत्वाकर्षण",
    "Work and Energy":"कार्य तथा ऊर्जा", "Sound":"ध्वनि", "Improvement in Food Resources":"खाद्य संसाधनों में सुधार",
    "Chemical Reactions and Equations":"रासायनिक अभिक्रियाएँ एवं समीकरण", "Acids, Bases and Salts":"अम्ल, क्षारक एवं लवण",
    "Metals and Non-metals":"धातु एवं अधातु", "Carbon and its Compounds":"कार्बन एवं उसके यौगिक", "Life Processes":"जैव प्रक्रम",
    "Control and Coordination":"नियंत्रण एवं समन्वय", "How do Organisms Reproduce?":"जीव जनन कैसे करते हैं?", "Heredity":"आनुवंशिकता",
    "Electricity":"विद्युत", "Our Environment":"हमारा पर्यावरण", "The French Revolution":"फ्रांसीसी क्रांति",
    "The Fun They Had":"वे दिन भी क्या दिन थे", "The Sound of Music":"संगीत की ध्वनि",
    "The Little Girl":"छोटी लड़की", "A Truly Beautiful Mind":"एक सचमुच सुंदर मस्तिष्क",
    "The Snake and the Mirror":"साँप और दर्पण", "My Childhood":"मेरा बचपन",
    "Reach for the Top":"शिखर तक पहुँचना", "Kathmandu":"काठमांडू", "If I Were You":"यदि मैं तुम्हारी जगह होता",
    "The Lake Isle of Innisfree":"इनिसफ्री झील का द्वीप", "A Legend of the Northland":"उत्तर देश की एक लोककथा",
    "No Men Are Foreign":"कोई भी मनुष्य पराया नहीं", "On Killing a Tree":"एक पेड़ की हत्या पर",
    "A Slumber Did My Spirit Seal":"एक निद्रा ने मेरी आत्मा को बाँध दिया",
    "The Adventures of Toto":"टोटो के कारनामे", "Iswaran the Storyteller":"कहानीकार ईश्वरन",
    "In the Kingdom of Fools":"मूर्खों के राज्य में", "The Happy Prince":"सुखी राजकुमार",
    "Weathering the Storm in Ersama":"एरसामा में तूफान का सामना", "The Last Leaf":"अंतिम पत्ती",
    "A House Is Not a Home":"घर केवल मकान नहीं", "The Beggar":"भिखारी",
    "India – Size and Location":"भारत: आकार और स्थिति", "Physical Features of India":"भारत के भौतिक स्वरूप",
    "Drainage":"अपवाह", "Climate":"जलवायु", "Natural Vegetation and Wildlife":"प्राकृतिक वनस्पति तथा वन्य प्राणी",
    "Population":"जनसंख्या", "Constitutional Design":"संविधान निर्माण", "Electoral Politics":"चुनावी राजनीति",
    "Working of Institutions":"संस्थाओं का कामकाज", "Democratic Rights":"लोकतांत्रिक अधिकार",
    "Poverty as a Challenge":"निर्धनता: एक चुनौती", "The Rise of Nationalism in Europe":"यूरोप में राष्ट्रवाद का उदय",
    "Socialism in Europe and the Russian Revolution":"यूरोप में समाजवाद एवं रूसी क्रांति",
    "Nazism and the Rise of Hitler":"नात्सीवाद और हिटलर का उदय", "Forest Society and Colonialism":"वन समाज और उपनिवेशवाद",
    "Pastoralists in the Modern World":"आधुनिक विश्व में चरवाहे", "The Making of a Global World":"भूमंडलीकृत विश्व का बनना",
    "What is Democracy? Why Democracy?":"लोकतंत्र क्या है? लोकतंत्र क्यों?", "Arithmetic Progressions":"समांतर श्रेढ़ियाँ", "Quadratic Equations":"द्विघात समीकरण", "Real Numbers":"वास्तविक संख्याएँ",
    "Nationalism in India":"भारत में राष्ट्रवाद", "The Age of Industrialisation":"औद्योगीकरण का युग",
    "Print Culture and the Modern World":"मुद्रण संस्कृति और आधुनिक दुनिया", "Forest and Wildlife Resources":"वन एवं वन्यजीव संसाधन",
    "Water Resources":"जल संसाधन", "Agriculture":"कृषि", "Minerals and Energy Resources":"खनिज तथा ऊर्जा संसाधन",
    "Manufacturing Industries":"विनिर्माण उद्योग", "Lifelines of National Economy":"राष्ट्रीय अर्थव्यवस्था की जीवन रेखाएँ",
    "Gender, Religion and Caste":"लैंगिकता, धर्म और जाति", "Political Parties":"राजनीतिक दल",
    "Outcomes of Democracy":"लोकतंत्र के परिणाम", "Sectors of the Indian Economy":"भारतीय अर्थव्यवस्था के क्षेत्र",
    "Globalisation and the Indian Economy":"वैश्वीकरण और भारतीय अर्थव्यवस्था",
    "Consumer Rights (Project Work / Evaluation)":"उपभोक्ता अधिकार (परियोजना कार्य / मूल्यांकन)",
    "The Proposal":"प्रस्ताव", "From the Diary of Anne Frank":"ऐन फ्रैंक की डायरी से",
    "Two Stories about Flying":"उड़ान के बारे में दो कहानियाँ", "Glimpses of India":"भारत की झलकियाँ",
    "Mijbil the Otter":"मिजबिल ऊदबिलाव", "Madam Rides the Bus":"मैडम बस की सवारी करती हैं",
    "The Sermon at Benares":"बनारस में उपदेश", "Nelson Mandela: Long Walk to Freedom":"नेल्सन मंडेला: स्वतंत्रता की लंबी यात्रा",
    "Dust of Snow":"बर्फ के कण", "Fire and Ice":"आग और बर्फ", "A Tiger in the Zoo":"चिड़ियाघर में बाघ",
    "How to Tell Wild Animals":"जंगली जानवरों की पहचान कैसे करें", "The Ball Poem":"गेंद पर कविता",
    "Amanda!":"अमांडा!", "The Trees":"पेड़", "Fog":"कोहरा", "The Tale of Custard the Dragon":"कस्टर्ड ड्रैगन की कहानी",
    "For Anne Gregory":"ऐन ग्रेगरी के लिए", "A Triumph of Surgery":"शल्य चिकित्सा की सफलता",
    "The Thief’s Story":"चोर की कहानी", "The Midnight Visitor":"आधी रात का मेहमान",
    "A Question of Trust":"विश्वास का प्रश्न", "Footprints without Feet":"बिना पैरों के पदचिह्न",
    "The Making of a Scientist":"एक वैज्ञानिक का निर्माण", "The Necklace":"हार", "Bholi":"भोली",
    "The Book That Saved the Earth":"वह पुस्तक जिसने पृथ्वी को बचाया",
    "Light – Reflection and Refraction":"प्रकाश: परावर्तन तथा अपवर्तन",
    "The Human Eye and the Colorful World":"मानव नेत्र तथा रंग-बिरंगा संसार",
    "Magnetic Effects of Electric Current":"विद्युत धारा के चुंबकीय प्रभाव",
    "How do Organisms Reproduce?":"जीव जनन कैसे करते हैं?",
    "Introduction to Trigonometry":"त्रिकोणमिति का परिचय", "Some Applications of Trigonometry":"त्रिकोणमिति के कुछ अनुप्रयोग",
    "Area Related to Circles":"वृत्तों से संबंधित क्षेत्रफल", "The Proposal":"प्रस्ताव",
    "The Lost Child":"खोया हुआ बच्चा", "The Midnight Visitor":"आधी रात का मेहमान",
    "A Letter to God":"ईश्वर को पत्र", "Resources and Development":"संसाधन एवं विकास", "Power Sharing":"सत्ता की साझेदारी",
    "Federalism":"संघवाद", "Development":"विकास", "Money and Credit":"मुद्रा और साख",
    "The Rise of Nationalism in Europe":"यूरोप में राष्ट्रवाद का उदय", "The Making of a Global World":"भूमंडलीकृत विश्व का बनना",
    "The Age of Industrialisation":"औद्योगीकरण का युग", "Print Culture and the Modern World":"मुद्रण संस्कृति और आधुनिक दुनिया",
    "The Story of Village Palampur":"पालमपुर गाँव की कहानी", "People as Resource":"लोग संसाधन के रूप में",
    "Food Security in India":"भारत में खाद्य सुरक्षा", "Wind":"हवा", "Rain on the Roof":"छत पर वर्षा",
    "The Road Not Taken":"जो रास्ता नहीं चुना गया", "A Letter to God":"ईश्वर को पत्र", "The Lost Child":"खोया हुआ बच्चा",
}

HINDI_NOTE_LABELS = {
    "Core idea":"मुख्य विचार", "Key terms":"मुख्य शब्द", "Important facts":"महत्वपूर्ण तथ्य",
    "How to solve":"हल करने की विधि", "Formulae":"सूत्र", "Diagram / map":"चित्र / मानचित्र",
    "Examples":"उदाहरण", "Applications":"अनुप्रयोग", "Quick revision":"त्वरित पुनरावृत्ति",
    "Definitions":"परिभाषाएँ", "Causes":"कारण", "Effects":"प्रभाव", "Features":"विशेषताएँ",
    "Process":"प्रक्रिया", "Comparison":"तुलना", "Remember":"याद रखें", "Practice":"अभ्यास",
    "Real numbers":"वास्तविक संख्याएँ", "Rational / irrational":"परिमेय / अपरिमेय", "Prime factorisation":"अभाज्य गुणनखंडन",
    "Decimal expansion":"दशमलव प्रसार", "Number line":"संख्या रेखा", "Polynomial":"बहुपद", "Terms & degree":"पद एवं घात",
    "Zeroes":"शून्यक", "Factorisation":"गुणनखंडन", "Standard form":"मानक रूप", "Quadratic formula":"द्विघात सूत्र",
    "Discriminant":"विविक्तकर", "Nature of roots":"मूलों की प्रकृति", "Sequence":"अनुक्रम", "First term a":"प्रथम पद a",
    "Common difference d":"सार्व अंतर d", "nth term":"nवाँ पद", "Sum Sₙ":"योग Sₙ", "Applications":"अनुप्रयोग",
    "Cartesian plane":"कार्तीय तल", "Axes & origin":"अक्ष एवं मूलबिंदु", "Ordered pair":"क्रमित युग्म",
    "Quadrants":"चतुर्थांश", "Plotting points":"बिंदुओं का आलेखन", "Distance / midpoint":"दूरी / मध्यबिंदु",
    "Right triangle":"समकोण त्रिभुज", "Six ratios":"छह अनुपात", "Standard values":"मानक मान", "Identities":"सर्वसमिकाएँ",
    "Ecosystem":"पारितंत्र", "Producers":"उत्पादक", "Consumers":"उपभोक्ता", "Decomposers":"अपघटक",
    "Food chain / web":"खाद्य श्रृंखला / जाल", "Context":"संदर्भ", "Plot / central idea":"कथानक / केंद्रीय विचार",
    "Characters / speaker":"पात्र / वक्ता", "Key events / images":"मुख्य घटनाएँ / बिंब", "Theme & message":"विषयवस्तु एवं संदेश",
    "Language & evidence":"भाषा एवं पाठ-साक्ष्य",
}

def get_chapter_study_pack(grade, subject, chapter):
    """Return a chapter-specific bilingual revision pack and mind map.

    Content is original study support aligned to the chapter title, not a reproduction of NCERT text
    and not a claim that generated practice questions are official CBSE PYQs.
    """
    lang = session.get("language", "en")
    grade_label = "Class IX" if grade == "9" else "Class X"
    facts = CHAPTER_FACTS.get(chapter)
    if facts:
        summary, points, branches = facts
    elif subject.startswith("English"):
        summary = f"Read “{chapter}” for its plot or central idea, characters or speaker, setting, conflict, and message. Support each interpretation with details from the prescribed text."
        points = [
            "Identify the setting, narrator or speaker, and the situation at the beginning.",
            "Track the central conflict, important events or images, and how the text develops.",
            "Note the traits, choices and relationships of important characters where relevant.",
            "For poetry, explain the central idea, tone, imagery, symbolism, rhyme or sound devices where present.",
            "Prepare a short summary, theme-based answer and evidence-based character or poetic analysis."
        ]
        branches = ["Context & setting", "Plot / central idea", "Characters / speaker", "Key events / images", "Theme & message", "Language & evidence"]
    elif subject == "Mathematics":
        summary = f"Revise the definitions, properties and standard methods used in {chapter}. Select the appropriate theorem or formula, show each step and verify the result."
        points = ["Write all definitions and conditions before applying a result.", "Make a labelled diagram or organise the given data when useful.", "Choose the relevant property, identity, theorem or formula.", "Show calculations in logical steps and retain units where required.", "Check the answer by substitution, estimation or an alternate method."]
        branches = ["Definitions", "Properties", "Theorem / formula", "Worked method", "Common errors", "Applications"]
    elif subject == "Science":
        summary = f"Study the scientific principles behind {chapter}, connect each process with its cause and effect, and practise labelled diagrams, equations and applications."
        points = ["Learn the key scientific terms and state each definition accurately.", "Understand the underlying principle and explain the process in sequence.", "Practise labelled diagrams, equations, units and observations where relevant.", "Compare related concepts and explain cause-and-effect relationships.", "Connect the concept to an experiment, daily-life example or application."]
        branches = ["Key concept", "Definitions", "Principle / process", "Diagram / equation", "Examples", "Applications"]
    elif subject in ("History", "Geography", "Political Science", "Economics"):
        summary = f"Understand {chapter} through its key concepts, context, examples and consequences. Organise long answers using clear headings and relevant evidence from the prescribed textbook."
        points = ["Define the central terms and explain the background or context.", "Organise the main ideas, events, features or stages in a logical sequence.", "Learn important examples, places, institutions, people or data from the chapter.", "Explain causes, effects, advantages, challenges or relationships as relevant.", "Practise map work, case studies, comparisons and structured answers where applicable."]
        branches = ["Context", "Key terms", "Main ideas / events", "Examples / case study", "Causes & effects", "Map / answer practice"]
    else:
        summary = f"Use this chapter guide for {chapter} as a structured revision path. Match each point with the relevant section and exercise in your school's prescribed textbook."
        points = ["Identify the chapter's key terms and learning objectives.", "Summarise the main concepts in your own words.", "Record important examples, diagrams, dates, formulae or definitions.", "Practise short-answer and application-based questions.", "Review mistakes and revisit the relevant textbook section."]
        branches = ["Overview", "Key terms", "Main concepts", "Examples", "Applications", "Revision"]

    # Turn every checklist item into a readable long-form learning paragraph.
    # The expansion explains how to understand, organise and apply the stated point;
    # it does not introduce unsupported textbook facts.
    detailed_points = []
    for item in points:
        if lang == "hi":
            detailed_points.append(
                f"{item}\n\nइसे केवल याद करने के बजाय अपने शब्दों में समझाइए। पहले इस बिंदु का अर्थ और अध्याय में इसकी भूमिका लिखें, फिर निर्धारित पाठ्यपुस्तक से संबंधित उदाहरण, चित्र, सूत्र या प्रमाण जोड़ें। उत्तर तैयार करते समय मुख्य शब्दों को स्पष्ट रखें और विचारों को कारण, प्रक्रिया तथा परिणाम के क्रम में व्यवस्थित करें। अंत में एक प्रश्न बनाकर बिना पुस्तक देखे उत्तर लिखें और फिर पाठ्यपुस्तक से मिलान करके छूटी हुई जानकारी सुधारें।"
            )
        elif subject == "Mathematics" or subject == "Maths":
            detailed_points.append(
                f"{item}\n\nइस अवधारणा को हल किए गए उदाहरण के साथ पढ़ें। सबसे पहले प्रश्न में दी गई जानकारी और ज्ञात करना क्या है, उसे अलग-अलग लिखें। इसके बाद लागू परिभाषा, गुण, प्रमेय या सूत्र की शर्तें जाँचें और प्रत्येक गणितीय चरण का कारण लिखते हुए हल आगे बढ़ाएँ। अंतिम उत्तर को मूल प्रश्न में रखकर या किसी वैकल्पिक जाँच से सत्यापित करें। अपनी कॉपी में एक ऐसा नया प्रश्न भी हल करें जिसमें यही विचार थोड़े अलग रूप में उपयोग हो।"
            )
        elif subject == "Science":
            detailed_points.append(
                f"{item}\n\nइस बिंदु को वैज्ञानिक अवधारणा के रूप में समझें: संबंधित शब्दों की परिभाषा लिखें, फिर सिद्धांत या प्रक्रिया को उसके सही क्रम में समझाएँ। जहाँ उपयुक्त हो, कारण और परिणाम के बीच संबंध स्पष्ट करें तथा पाठ्यपुस्तक के चित्र, समीकरण, इकाई, प्रयोग या दैनिक जीवन के उदाहरण से इसे जोड़ें। अंत में तुलना या अनुप्रयोग वाला प्रश्न हल करें और जाँचें कि उत्तर में वैज्ञानिक शब्दावली तथा आवश्यक चरण शामिल हैं।"
            )
        elif subject in ("History", "Geography", "Political Science", "Economics", "Social Science"):
            detailed_points.append(
                f"{item}\n\nइस विषय को उसके ऐतिहासिक, भौगोलिक, सामाजिक या आर्थिक संदर्भ में समझें। पहले मुख्य विचार और उससे जुड़े शब्दों का अर्थ लिखें; फिर पाठ्यपुस्तक में दिए गए उदाहरण, स्थान, घटनाएँ, संस्थाएँ या आँकड़े जोड़ें। लंबे उत्तर में पृष्ठभूमि, मुख्य व्याख्या और प्रभाव/महत्त्व को अलग अनुच्छेदों में रखें। जहाँ प्रश्न तुलना, कारण या परिणाम पूछता है, वहाँ प्रत्येक बिंदु को प्रमाणित उदाहरण से स्पष्ट करें और निर्धारित मानचित्र या केस-स्टडी अभ्यास भी पूरा करें।"
            )
        else:
            detailed_points.append(
                f"{item}\n\nइस विचार को अध्याय के पूरे संदर्भ से जोड़कर पढ़ें। मुख्य शब्दों का अर्थ स्पष्ट करें और यह पहचानें कि यह बिंदु केंद्रीय विषय, पात्र, घटना, तर्क या भाषा-शैली से कैसे जुड़ता है। निर्धारित पाठ से उपयुक्त उदाहरण या साक्ष्य चुनें और उसे अपने उत्तर में समझाकर उपयोग करें। अंत में सारांश तथा व्याख्यात्मक उत्तर लिखकर जाँचें कि उसमें मुख्य विचार, आवश्यक विवरण और निष्कर्ष तीनों शामिल हैं।"
            )
    points = detailed_points

    practice = [
        f"Explain the central concept or argument in “{chapter}” with two relevant details.",
        f"Create a labelled flowchart, diagram, timeline or worked example that summarises “{chapter}”.",
        f"Write one application-based question from “{chapter}” and solve it step by step."
    ]
    title_hi = HINDI_TITLES.get(chapter, chapter)
    subject_hi = HINDI_TITLES.get(subject, subject.replace("English —", "अंग्रेज़ी —"))
    if lang == "hi":
        overview = f"कक्षा {grade} के {subject_hi} विषय में ‘{title_hi}’ का अध्ययन करें। नीचे दिए गए बिंदु अध्याय की अवधारणाओं को समझने और दोहराने के लिए व्यवस्थित किए गए हैं। अपनी निर्धारित पाठ्यपुस्तक के उदाहरणों और अभ्यासों के साथ इनका उपयोग करें।"
        if facts:
            # The core fact sheet is translated into concise Hindi revision language, retaining standard formulae.
            hi_points = {
                "Number Systems": ["प्राकृतिक, पूर्ण, पूर्णांक, परिमेय, अपरिमेय और वास्तविक संख्याओं में अंतर पहचानें।", "अभाज्य गुणनखंडन और अंकगणित की आधारभूत प्रमेय का उपयोग करें।", "अपरिमेय संख्याओं को संख्या रेखा पर दर्शाएँ और करणी सरल करें।", "परिमेय संख्या p/q के सरलतम रूप में दशमलव प्रसार तभी सांत होगा जब q के अभाज्य गुणनखंड केवल 2 और 5 हों।"],
                "Polynomials": ["पद, गुणांक, घात और अचर पद पहचानें।", "गुणनखंडन या निरीक्षण से शून्यक ज्ञात करें।", "ax²+bx+c के लिए शून्यकों का योग −b/a तथा गुणनफल c/a होता है।", "बहुपद के ग्राफ के x-अक्ष को काटने वाले बिंदु उसके शून्यक दर्शाते हैं।"],
                "Quadratic Equations": ["गुणनखंडन, पूर्ण वर्ग बनाने या द्विघात सूत्र से हल करें।", "विविक्तकर D=b²−4ac मूलों की प्रकृति बताता है।", "D>0: दो भिन्न वास्तविक मूल; D=0: समान वास्तविक मूल; D<0: कोई वास्तविक मूल नहीं।", "मूलों को मूल समीकरण में रखकर जाँचें।"],
                "Arithmetic Progressions": ["सार्व अंतर d=a₂−a₁ ज्ञात करें।", "nवाँ पद: aₙ=a+(n−1)d।", "पहले n पदों का योग: Sₙ=n/2[2a+(n−1)d]।", "सूत्र लगाने से पहले प्रथम पद, सार्व अंतर और पदों की संख्या पहचानें।"],
                "Chemical Reactions and Equations": ["अभिक्रिया के संकेतों में रंग परिवर्तन, गैस निकलना, अवक्षेप बनना या ताप परिवर्तन शामिल हो सकते हैं।", "समीकरण को गुणांकों से संतुलित करें; रासायनिक सूत्र के नीचे लिखे अंक न बदलें।", "संयोजन, अपघटन, विस्थापन और द्विविस्थापन अभिक्रियाएँ पहचानें।", "ऑक्सीकरण और अपचयन की अवधारणा उदाहरणों सहित समझें।"],
                "Acids, Bases and Salts": ["अम्ल जलीय विलयन में H⁺/H₃O⁺ आयन तथा क्षारक OH⁻ आयन देते हैं।", "pH 7 से कम अम्लीय, 7 उदासीन और 7 से अधिक क्षारीय होता है।", "उदासीनीकरण: अम्ल + क्षारक → लवण + जल।", "बेकिंग सोडा, धुलाई सोडा, विरंजक चूर्ण और प्लास्टर ऑफ पेरिस के उपयोग पढ़ें।"],
                "Motion": ["चाल = दूरी/समय; वेग = विस्थापन/समय।", "त्वरण = वेग में परिवर्तन/समय।", "समान त्वरण के समीकरण: v=u+at; s=ut+½at²; v²=u²+2as।", "दूरी-समय ग्राफ की ढाल चाल और वेग-समय ग्राफ की ढाल त्वरण देती है।"],
                "Electricity": ["I=Q/t; विभवांतर V=W/Q।", "ओम का नियम: V=IR (स्थिर भौतिक परिस्थितियों में)।", "श्रेणीक्रम में प्रतिरोध जुड़ते हैं; समानांतर क्रम में प्रतिरोधों के व्युत्क्रम जुड़ते हैं।", "शक्ति P=VI=I²R=V²/R; विद्युत ऊर्जा जूल या किलोवाट-घंटा में मापी जाती है।"],
                "Life Processes": ["पोषण, श्वसन, परिवहन और उत्सर्जन जीवन बनाए रखने वाली प्रमुख प्रक्रियाएँ हैं।", "स्वपोषी प्रकाश ऊर्जा से भोजन बनाते हैं; परपोषी अन्य जीवों से भोजन प्राप्त करते हैं।", "वायवीय श्वसन अवायवीय श्वसन से अधिक ऊर्जा मुक्त करता है।", "मनुष्य में हृदय, रक्त और रक्त वाहिकाएँ परिवहन करते हैं; पौधों में जाइलम और फ्लोएम यह कार्य करते हैं।"],
                "Power Sharing": ["बेल्जियम और श्रीलंका विविधता तथा सत्ता-साझेदारी के अलग-अलग उदाहरण हैं।", "क्षैतिज सत्ता-साझेदारी विधायिका, कार्यपालिका और न्यायपालिका के बीच होती है।", "ऊर्ध्वाधर सत्ता-साझेदारी केंद्र, राज्य और स्थानीय सरकारों के बीच होती है।", "सामाजिक समूहों, राजनीतिक दलों और दबाव समूहों के बीच भी सत्ता साझा हो सकती है।"],
                "Federalism": ["संघवाद में संविधान द्वारा सरकार के विभिन्न स्तरों के बीच शक्तियों का विभाजन होता है।", "भारत में संघ, राज्य और स्थानीय सरकार के स्तर हैं।", "विधायी विषय संघ सूची, राज्य सूची और समवर्ती सूची में बाँटे गए हैं।", "विकेंद्रीकरण पंचायतों और नगरपालिकाओं के माध्यम से स्थानीय भागीदारी बढ़ाता है।"],
                "Development": ["विकास के लक्ष्य अलग-अलग लोगों के लिए अलग हो सकते हैं; इसमें आय के साथ स्वास्थ्य, शिक्षा, सुरक्षा और सम्मान भी शामिल हैं।", "प्रति व्यक्ति आय औसत आय है, लेकिन यह कल्याण का पूरा माप नहीं है।", "साक्षरता, जीवन प्रत्याशा और शिशु मृत्यु दर जैसे संकेतकों से तुलना करें।", "सार्वजनिक सुविधाएँ और समान अवसर जीवन की गुणवत्ता को प्रभावित करते हैं।"],
            }.get(chapter)
            if hi_points:
                points = hi_points
            else:
                points = [f"‘{title_hi}’ के मुख्य शब्दों और परिभाषाओं को समझें।", f"‘{title_hi}’ की प्रमुख अवधारणाओं और उनके आपसी संबंधों को क्रम से लिखें।", "पाठ्यपुस्तक के चित्र, उदाहरण, सूत्र, मानचित्र या समयरेखा का अभ्यास करें।", "कारण, प्रभाव, तुलना और वास्तविक जीवन के अनुप्रयोग पर आधारित प्रश्न हल करें।", "अध्याय के अभ्यास प्रश्न हल करके अपनी गलतियों का पुनरावलोकन करें।"]
        else:
            points = [f"‘{title_hi}’ के मुख्य शब्दों और परिभाषाओं को समझें।", f"‘{title_hi}’ की प्रमुख घटनाओं, विचारों या प्रक्रियाओं को क्रम से लिखें।", "महत्वपूर्ण उदाहरण, पात्र, स्थान, तिथियाँ, चित्र या आँकड़े याद करें।", "कारण-परिणाम, तुलना और अनुप्रयोग आधारित प्रश्नों का अभ्यास करें।", "पाठ्यपुस्तक के अभ्यास प्रश्न हल करें और उत्तरों को दोहराएँ।"]
        branches_hi = [HINDI_NOTE_LABELS.get(x, x) for x in branches]
        practice_hi = [f"‘{title_hi}’ की मुख्य अवधारणा को दो प्रासंगिक बिंदुओं सहित समझाइए।", f"‘{title_hi}’ का सार दर्शाने वाला नामांकित चित्र, प्रवाह-चित्र, समयरेखा या हल किया हुआ उदाहरण बनाइए।", f"‘{title_hi}’ पर एक अनुप्रयोग-आधारित प्रश्न बनाकर चरणबद्ध हल कीजिए।"]
        return {"intro":f"कक्षा {grade} · {subject_hi} · अध्यायवार अध्ययन सामग्री", "display_chapter":title_hi, "overview":overview, "notes":points, "mindmap":branches_hi, "practice":practice_hi, "caution":"ये मौलिक पुनरावृत्ति नोट्स और अभ्यास प्रश्न हैं; इन्हें आधिकारिक NCERT पाठ या सत्यापित पिछले-वर्ष के प्रश्न न समझें। आधिकारिक PYQ के लिए CBSE के प्रकाशित प्रश्नपत्र देखें।"}

    overview = summary + " Use the matching NCERT chapter and your teacher's prescribed scope for full coverage."
    return {"intro":f"{grade_label} · {subject} · chapter learning pack", "display_chapter":chapter, "overview":overview, "notes":points, "mindmap":branches, "practice":practice, "caution":"These are original revision notes and practice prompts, not reproduced NCERT chapters or verified official PYQs. Check your prescribed textbook and official CBSE sample papers for complete coverage and authentic previous-year questions."}


DEFAULT_SUBJECT_META = {
    "Science":         {"icon": "SCI", "color_from": "#A9DFFF", "color_to": "#788A86", "tag": "PHY · CHM · BIO"},
    "Social Science":  {"icon": "SST", "color_from": "#A69B8B", "color_to": "#817D75", "tag": "HIS · GEO · POL · ECO"},
    "Maths":           {"icon": "MTH", "color_from": "#9CCFEA", "color_to": "#788A86", "tag": "ALG · GEO · TRIG · STAT"},
    "Hindi & English": {"icon": "LIT", "color_from": "#82BCE0", "color_to": "#75BFEA", "tag": "LIT · PROSE · POETRY"}
}

COLOR_PRESETS = [
    {"from": "#A9DFFF", "to": "#75BFEA", "name": "Ice Blue"},
    {"from": "#9CCFEA", "to": "#788A86", "name": "Glacier Blue"},
    {"from": "#82BCE0", "to": "#75BFEA", "name": "Arctic Blue"},
    {"from": "#B7E5FF", "to": "#FF6B00", "name": "Silver Blue"},
    {"from": "#A9DFFF", "to": "#9CCFEA", "name": "Frosted Sky"},
    {"from": "#A9DFFF", "to": "#75BFEA", "name": "Deep Ocean"},
]

ICON_PRESETS = ['', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '']

AVATAR_COLORS = ['#A9DFFF', '#9CCFEA', '#82BCE0', '#9B968D', '#788A86', '#B7E5FF']

CIRCLES_PER_CHAPTER = 5
CIRCLE_LABELS = ["Read", "Note", "Prac", "Rev", "Done"]

CHAT_ROOMS = [
    {"id": "general", "name": "General", "icon": "ST", "desc": "Chat about anything"},
    {"id": "science", "name": "Science", "icon": "ST", "desc": "Physics, Chem, Bio"},
    {"id": "maths", "name": "Maths", "icon": "ST", "desc": "Algebra, geometry, trig"},
    {"id": "social", "name": "Social", "icon": "ST", "desc": "History, geo, civics"},
    {"id": "english", "name": "Lang", "icon": "ST", "desc": "Hindi & English lit"},
    {"id": "doubts", "name": "Doubts", "icon": "ST", "desc": "Ask & help others"},
]


# 
# HELPERS
# 

def get_current_user():
    if 'user_id' not in session:
        return None
    return User.query.get(session['user_id'])


def touch_user(user):
    if user:
        now = datetime.utcnow()
        # Throttle presence writes: repeated page refreshes should not each force a DB commit.
        if user.last_seen and (now - user.last_seen).total_seconds() < 60:
            return
        try:
            user.last_seen = now
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
    # Fetch all custom chapters in one query (avoid one SQL query per subject).
    custom_ids = [cs.id for cs in customs]
    chapters_by_subject = {sid: [] for sid in custom_ids}
    if custom_ids:
        custom_chapters = (CustomChapter.query
            .filter(CustomChapter.subject_id.in_(custom_ids))
            .order_by(CustomChapter.subject_id, CustomChapter.order_index, CustomChapter.id).all())
        for chapter in custom_chapters:
            chapters_by_subject[chapter.subject_id].append(chapter.name)
    for cs in customs:
        curriculum[cs.name] = {
            "chapters": chapters_by_subject.get(cs.id, []),
            "meta": {"icon": cs.icon, "color_from": cs.color_from, "color_to": cs.color_to, "tag": cs.tag},
            "is_custom": True,
            "subject_id": cs.id
        }
    return curriculum


def get_user_progress(user_id):
    rows = ChapterProgress.query.filter_by(user_id=user_id, completed=True).all()
    return {f"{r.subject}__{r.chapter_index}__{r.circle_index}": True for r in rows}


def compute_stats(user_id, curriculum=None):
    curriculum = curriculum if curriculum is not None else get_full_curriculum(user_id)
    total_circles = sum(len(v['chapters']) * CIRCLES_PER_CHAPTER for v in curriculum.values())
    completed_rows = ChapterProgress.query.filter_by(user_id=user_id, completed=True).count()
    overall_pct = round((completed_rows / total_circles) * 100, 1) if total_circles else 0

    # One grouped query replaces repeated per-subject COUNT queries.
    grouped_done = dict(
        db.session.query(ChapterProgress.subject, func.count(ChapterProgress.id))
        .filter_by(user_id=user_id, completed=True)
        .group_by(ChapterProgress.subject).all()
    )
    active_chambers = 0
    subject_pcts = {}
    subject_done_circles = {}
    for subject, info in curriculum.items():
        subj_total = len(info['chapters']) * CIRCLES_PER_CHAPTER
        subj_done = grouped_done.get(subject, 0)
        pct = round((subj_done / subj_total) * 100, 1) if subj_total else 0
        subject_pcts[subject] = pct
        subject_done_circles[subject] = subj_done
        if subj_done > 0:
            active_chambers += 1

    fully_done_chapters = 0
    total_chapters = sum(len(v['chapters']) for v in curriculum.values())
    chapter_counts = db.session.query(
        ChapterProgress.subject, ChapterProgress.chapter_index, func.count(ChapterProgress.id)
    ).filter_by(user_id=user_id, completed=True).group_by(
        ChapterProgress.subject, ChapterProgress.chapter_index
    ).all()
    chapter_done_lookup = {(subject, index): count for subject, index, count in chapter_counts}
    for subject, info in curriculum.items():
        for ci in range(len(info['chapters'])):
            if chapter_done_lookup.get((subject, ci), 0) == CIRCLES_PER_CHAPTER:
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
    start_day = today - timedelta(days=6)
    rows = ActivityLog.query.filter(
        ActivityLog.user_id == user_id,
        ActivityLog.activity_date >= start_day,
        ActivityLog.activity_date <= today
    ).all()
    counts = {row.activity_date: (row.circles_completed or 0) for row in rows}
    return [{
        "date": (d := today - timedelta(days=i)).strftime('%d %b'),
        "day_label": d.strftime('%a'),
        "count": counts.get(d, 0),
        "is_today": d == today
    } for i in range(6, -1, -1)]


def compute_exam_plans(user_id, curriculum=None, stats=None):
    curriculum = curriculum if curriculum is not None else get_full_curriculum(user_id)
    stats = stats if stats is not None else compute_stats(user_id, curriculum)
    exams = ExamSchedule.query.filter_by(user_id=user_id).all()
    # Aggregate all completed circles once; avoid an exam x chapter N+1 query loop.
    chapter_done_rows = db.session.query(
        ChapterProgress.subject, ChapterProgress.chapter_index,
        func.count(ChapterProgress.id)
    ).filter_by(user_id=user_id, completed=True).group_by(
        ChapterProgress.subject, ChapterProgress.chapter_index
    ).all()
    chapter_done_lookup = {(subject, index): count for subject, index, count in chapter_done_rows}
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

        remaining_chapters = sum(
            1 for ci in range(total_ch)
            if chapter_done_lookup.get((exam.subject, ci), 0) < CIRCLES_PER_CHAPTER
        )

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


# 
# BASE STYLE
# 

BASE_STYLE = """
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0"/>
<meta name="theme-color" content="#031130"/>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
  tailwind.config = {
    theme: { extend: {
      colors: {
        'app-bg': '#080F17', 'card-bg': '#101C29',
        'cyan-neon': '#A9DFFF', 'purple-neon': '#75BFEA',
        'green-neon': '#9CCFEA', 'muted': '#9A9B98'
      },
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] }
    }}
  }
</script>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet"/>
<style>
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { background:#080F17; font-family:'Inter',sans-serif; color:#E7F5FF; min-height:100vh; overflow-x:hidden; }
  a { text-decoration:none; }
  .glass-nav { background:rgba(11,15,25,0.92); backdrop-filter:blur(24px); border-top:1px solid rgba(145,215,255,0.12); }
  .gradient-text { background:linear-gradient(135deg,#A9DFFF,#75BFEA); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
  .gradient-btn { background:linear-gradient(135deg,#A9DFFF,#75BFEA); transition:all 0.3s ease; }
  .gradient-btn:hover { opacity:0.92; transform:translateY(-1px); box-shadow:0 8px 25px rgba(145,215,255,0.3); }
  .gradient-btn:disabled { opacity:0.5; cursor:not-allowed; }
  .card-base { background:#101C29; border:1px solid rgba(255,255,255,0.06); border-radius:16px; transition:all 0.25s ease; }
  .card-glow:hover { box-shadow:0 0 30px rgba(145,215,255,0.08), 0 8px 32px rgba(0,0,0,0.4); transform:translateY(-2px); }
  .circle-btn { transition:all 0.2s cubic-bezier(0.34,1.56,0.64,1); cursor:pointer; user-select:none; }
  .circle-btn:hover { transform:scale(1.12); }
  .circle-btn:active { transform:scale(0.94); }
  .nav-icon { transition:all 0.2s ease; }
  .nav-icon:hover { transform:translateY(-2px); }
  .scroll-content { padding-bottom:100px; }
  ::-webkit-scrollbar { width:4px; }
  ::-webkit-scrollbar-track { background:#080F17; }
  ::-webkit-scrollbar-thumb { background:#101C29; border-radius:4px; }
  .input-field { background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.1); border-radius:12px; color:#E7F5FF; outline:none; transition:all 0.2s ease; }
  .input-field:focus { border-color:#A9DFFF; box-shadow:0 0 0 3px rgba(145,215,255,0.12); }
  .chapter-row { background:rgba(255,255,255,0.02); border:1px solid rgba(255,255,255,0.05); border-radius:14px; transition:all 0.2s ease; }
  .chapter-row:hover { background:rgba(255,255,255,0.04); border-color:rgba(145,215,255,0.15); }
  .tag-pill { background:rgba(255,255,255,0.06); border-radius:999px; }
  @keyframes fadeInUp { from{opacity:0;transform:translateY(20px)} to{opacity:1;transform:translateY(0)} }
  .fade-in { animation:fadeInUp 0.5s ease forwards; }
  @keyframes msgIn { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
  .msg-in { animation:msgIn 0.3s ease forwards; }
  @keyframes pulseGlow { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.5;transform:scale(1.2)} }
  .online-pulse { animation:pulseGlow 2s ease infinite; }
  @keyframes typingDot { 0%,60%,100%{transform:translateY(0);opacity:0.4} 30%{transform:translateY(-4px);opacity:1} }
  .typing-dot { display:inline-block; width:6px; height:6px; border-radius:50%; background:#A9DFFF; margin:0 2px; animation:typingDot 1.4s infinite; }
  .typing-dot:nth-child(2){animation-delay:0.15s;} .typing-dot:nth-child(3){animation-delay:0.3s;}
  .subject-card { border-radius:20px; overflow:hidden; position:relative; transition:all 0.3s ease; cursor:pointer; }
  .subject-card:hover { transform:translateY(-4px); box-shadow:0 20px 40px rgba(0,0,0,0.5); }
  .modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.7); backdrop-filter:blur(6px); z-index:100; display:none; align-items:center; justify-content:center; padding:16px; }
  .modal-overlay.active { display:flex; animation:fadeInUp 0.3s ease; }
  .modal-content { background:#101C29; border:1px solid rgba(255,255,255,0.08); border-radius:20px; width:100%; max-width:440px; max-height:90vh; overflow-y:auto; }
  .icon-choice, .color-choice { cursor:pointer; transition:all 0.2s ease; }
  .icon-choice:hover, .color-choice:hover { transform:scale(1.1); }
  .icon-choice.active { background:rgba(145,215,255,0.15) !important; border-color:#A9DFFF !important; }
  .color-choice.active { transform:scale(1.15); box-shadow:0 0 0 3px #fff; }
  .bar-chart-bar { transition:all 0.6s cubic-bezier(0.34,1.2,0.64,1); }
  .chapter-item { background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.06); border-radius:10px; padding:10px 12px; display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .btn-secondary { background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:10px; padding:8px 14px; font-size:13px; font-weight:600; color:#E7F5FF; cursor:pointer; transition:all 0.2s; }
  .btn-secondary:hover { background:rgba(255,255,255,0.08); border-color:rgba(145,215,255,0.3); }
  .btn-danger { background:rgba(255,100,100,0.08); border:1px solid rgba(255,100,100,0.3); color:#82BCE0; }
  .btn-danger:hover { background:rgba(255,100,100,0.15); }
  .exam-card { border-radius:18px; padding:18px; position:relative; overflow:hidden; }
  .countdown-badge { display:inline-flex; align-items:center; gap:4px; padding:4px 10px; border-radius:999px; font-size:11px; font-weight:800; }
  input[type="date"] { color-scheme:dark; }
  .avatar { width:36px; height:36px; border-radius:999px; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:12px; color:#080F17; flex-shrink:0; }
  .chat-bubble { border-radius:14px; padding:10px 14px; max-width:78%; word-wrap:break-word; line-height:1.4; font-size:13.5px; }
  .chat-bubble.mine { background:linear-gradient(135deg,#A9DFFF,#75BFEA); color:#fff; border-bottom-right-radius:4px; }
  .chat-bubble.theirs { background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.08); color:#E7F5FF; border-bottom-left-radius:4px; }
  .chat-scroll { height:calc(100vh - 340px); min-height:300px; overflow-y:auto; padding:8px 12px; scroll-behavior:smooth; }
  .chat-input-bar { position:fixed; bottom:78px; left:0; right:0; background:rgba(11,15,25,0.96); backdrop-filter:blur(20px); border-top:1px solid rgba(255,255,255,0.06); z-index:40; padding:10px 16px; }
  .reply-preview { background:rgba(145,215,255,0.06); border-left:3px solid #A9DFFF; border-radius:8px; padding:6px 10px; margin-bottom:8px; font-size:12px; display:flex; align-items:center; justify-content:space-between; }
  .room-pill { transition:all 0.2s ease; }
  .room-pill.active { background:linear-gradient(135deg,rgba(145,215,255,0.18),rgba(105,177,225,0.18)); border-color:#A9DFFF !important; color:#fff !important; }
  .status-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .status-dot.connected { background:#9CCFEA; box-shadow:0 0 8px rgba(0,242,96,0.5); }
  .status-dot.connecting { background:#B7E5FF; }
  .status-dot.disconnected { background:#82BCE0; }
  .join-toast { position:fixed; top:20px; left:50%; transform:translateX(-50%); background:rgba(26,32,53,0.95); border:1px solid rgba(145,215,255,0.3); border-radius:999px; padding:8px 18px; font-size:12px; font-weight:600; color:#E7F5FF; z-index:200; box-shadow:0 8px 24px rgba(0,0,0,0.5); backdrop-filter:blur(20px); }
  /* StudyTracker premium visual system: quiet neutrals, frosted surfaces, no RGB/neon. */
  body { background:#080F17; color:#E7F5FF; }
  .glass-nav { background:rgba(20,22,23,.88); border-top:1px solid rgba(169,223,255,.13); }
  .gradient-text { background:linear-gradient(120deg,#E3F5FF,#9BCFEF); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
  .gradient-btn { background:linear-gradient(135deg,#A9DFFF,#75BFEA); color:#171817; box-shadow:0 5px 18px rgba(0,0,0,.18); }
  .gradient-btn:hover { box-shadow:0 8px 24px rgba(145,215,255,.14); }
  .card-base,.modal-content { background:rgba(30,33,34,.82); border:1px solid rgba(169,223,255,.10); box-shadow:inset 0 1px 0 rgba(255,255,255,.025); }
  .card-glow:hover { box-shadow:0 14px 34px rgba(0,0,0,.24); }
  .input-field:focus { border-color:#A9DFFF; box-shadow:0 0 0 3px rgba(145,215,255,.10); }
  .chapter-row:hover,.btn-secondary:hover { border-color:rgba(145,215,255,.28); }
  .avatar { background:linear-gradient(145deg,rgba(169,223,255,.24),rgba(80,135,170,.12)); border:1px solid rgba(169,223,255,.30); color:#EAF7FF; box-shadow:inset 0 1px 0 rgba(255,255,255,.16); }
  .chat-bubble.mine { background:linear-gradient(135deg,#477FA3,#315A78); color:#EAF7FF; }
  .status-dot.connected { background:#9CCFEA; box-shadow:none; }
  .typing-dot { background:#A9DFFF; }
  .study-wrap { color:#E7F5FF; }
  .study-hero,.mind-node { background:rgba(169,223,255,.045) !important; border-color:rgba(169,223,255,.14) !important; color:#E7F5FF !important; }
  .study-tabs button.active { background:linear-gradient(135deg,#477FA3,#315A78); color:#EAF7FF; }
  .subject-card:hover { box-shadow:0 16px 34px rgba(0,0,0,.25); }
  @media (prefers-reduced-motion: reduce) { *,*::before,*::after { animation-duration:.01ms !important; transition-duration:.01ms !important; scroll-behavior:auto !important; } }

  /* Premium Arctic Blue visual system */
  :root { --ice:#A9DFFF; --ice-bright:#D9F2FF; --ice-mid:#75BFEA; --ice-deep:#397DA8; --surface:#101C29; }
  body { background:radial-gradient(ellipse at 50% -15%,rgba(105,177,225,.16),transparent 52%),#080F17 !important; color:#E7F5FF !important; }
  .glass-nav,.chat-input-bar { background:rgba(7,16,27,.88) !important; border-color:rgba(169,223,255,.22) !important; }
  .card-base,.modal-content { background:linear-gradient(145deg,rgba(20,35,49,.88),rgba(9,18,29,.94)) !important; border:1px solid rgba(169,223,255,.22) !important; box-shadow:inset 0 1px 0 rgba(230,248,255,.08),0 12px 32px rgba(0,0,0,.28) !important; }
  .card-glow:hover,.subject-card:hover { box-shadow:0 0 24px rgba(120,195,240,.16),0 16px 38px rgba(0,0,0,.34) !important; }
  .gradient-text { background:linear-gradient(115deg,#EAF8FF 0%,#A9DFFF 48%,#6DB8E7 100%) !important; -webkit-background-clip:text !important; background-clip:text !important; }
  .gradient-btn { color:#071521 !important; background:linear-gradient(120deg,#D8F2FF,#8CCDF2 55%,#69B5E5) !important; box-shadow:0 5px 22px rgba(105,181,229,.22),inset 0 1px 0 rgba(255,255,255,.55) !important; }
  .gradient-btn:hover { box-shadow:0 8px 28px rgba(105,181,229,.34) !important; }
  .avatar { color:#EAF8FF !important; background:linear-gradient(145deg,rgba(190,232,255,.24),rgba(72,139,184,.12)) !important; border:1px solid rgba(190,232,255,.42) !important; box-shadow:inset 0 1px 0 rgba(255,255,255,.24),0 0 18px rgba(105,181,229,.10) !important; }
  .input-field:focus { border-color:#A9DFFF !important; box-shadow:0 0 0 3px rgba(145,215,255,.16) !important; }
  .chapter-row:hover,.btn-secondary:hover,.room-pill.active { border-color:rgba(169,223,255,.48) !important; }
  .chat-bubble.mine,.study-tabs button.active { background:linear-gradient(120deg,#477FA3,#315A78) !important; color:#F0FAFF !important; }
  .study-hero,.mind-node { background:linear-gradient(145deg,rgba(169,223,255,.10),rgba(80,150,195,.045)) !important; border-color:rgba(169,223,255,.24) !important; }
  .status-dot.connected { background:#A9DFFF !important; box-shadow:0 0 9px rgba(145,215,255,.45) !important; }
  .typing-dot { background:#A9DFFF !important; }
  .tag-pill { background:rgba(145,215,255,.10) !important; border:1px solid rgba(145,215,255,.12); }
  /* Language packs are rendered as one language at a time; prevent automatic cross-translation of chapter content. */
  .chapter-notes-content { unicode-bidi:plaintext; }

  /* SalesPort-inspired premium blue + liquid-glass system */
  :root {
    --blue-950:#031130; --blue-900:#061943; --blue-800:#0A2B70;
    --blue-600:#185DF1; --blue-500:#3477FF; --blue-300:#8EB5FF;
    --ice-white:#F3F7FE; --glass-line:rgba(196,218,255,.22);
    --glass-fill:rgba(12,34,82,.48);
  }
  html { background:#020817; }
  body {
    background:
      radial-gradient(ellipse at 50% 110%,rgba(24,93,241,.34),transparent 48%),
      radial-gradient(ellipse at 92% 12%,rgba(24,93,241,.19),transparent 36%),
      linear-gradient(145deg,#020817 0%,#031130 54%,#020817 100%) !important;
    color:var(--ice-white) !important;
  }
  .glass-nav,.chat-input-bar {
    background:rgba(3,17,48,.68) !important;
    -webkit-backdrop-filter:blur(28px) saturate(165%);
    backdrop-filter:blur(28px) saturate(165%);
    border-color:rgba(142,181,255,.28) !important;
    box-shadow:0 -12px 38px rgba(0,0,0,.28),inset 0 1px 0 rgba(243,247,254,.09);
  }
  .card-base,.modal-content,.study-panel,.chapter-link,.chapter-row,.chapter-item,
  .exam-card,.subject-card,.room-pill,.tag-pill {
    background:linear-gradient(145deg,rgba(19,48,111,.56),rgba(3,17,48,.72)) !important;
    border:1px solid var(--glass-line) !important;
    -webkit-backdrop-filter:blur(22px) saturate(155%);
    backdrop-filter:blur(22px) saturate(155%);
    box-shadow:inset 0 1px 0 rgba(243,247,254,.12),inset 0 -1px 0 rgba(24,93,241,.12),0 14px 38px rgba(0,0,0,.24);
  }
  .card-glow:hover,.subject-card:hover,.chapter-link:hover {
    border-color:rgba(142,181,255,.52) !important;
    box-shadow:0 0 0 1px rgba(24,93,241,.18),0 0 34px rgba(24,93,241,.22),0 18px 42px rgba(0,0,0,.36) !important;
    transform:translateY(-2px);
  }
  .gradient-text {
    background:linear-gradient(110deg,#F3F7FE 0%,#BBD1FF 42%,#4C86FF 100%) !important;
    -webkit-background-clip:text !important;background-clip:text !important;
  }
  .gradient-btn {
    color:#fff !important;
    background:linear-gradient(115deg,#185DF1 0%,#3477FF 58%,#5C91FF 100%) !important;
    border:1px solid rgba(195,216,255,.34) !important;
    box-shadow:0 8px 26px rgba(24,93,241,.34),inset 0 1px 0 rgba(243,247,254,.28) !important;
  }
  .gradient-btn:hover {box-shadow:0 10px 34px rgba(24,93,241,.48),inset 0 1px 0 rgba(243,247,254,.32) !important;}
  .btn-secondary,.input-field,select,input,textarea {
    background:rgba(8,27,69,.62) !important;color:#F3F7FE !important;
    border:1px solid rgba(142,181,255,.25) !important;
    -webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);
  }
  select option {background:#031130;color:#F3F7FE;}
  .input-field:focus,select:focus,input:focus,textarea:focus {
    border-color:#4C86FF !important;box-shadow:0 0 0 3px rgba(24,93,241,.22),0 0 22px rgba(24,93,241,.16) !important;
  }
  .avatar {
    color:#F3F7FE !important;
    background:linear-gradient(145deg,rgba(24,93,241,.66),rgba(3,17,48,.82)) !important;
    border:1px solid rgba(190,211,255,.48) !important;
    box-shadow:inset 0 1px 0 rgba(243,247,254,.3),0 0 22px rgba(24,93,241,.25) !important;
  }
  .chat-bubble.mine,.study-tabs button.active,.room-pill.active {
    background:linear-gradient(120deg,#185DF1,#3477FF) !important;color:#fff !important;
    border:1px solid rgba(190,211,255,.36) !important;
    box-shadow:0 6px 20px rgba(24,93,241,.25),inset 0 1px 0 rgba(243,247,254,.22);
  }
  .study-hero,.subject-hero {
    background:radial-gradient(ellipse at 78% 5%,rgba(24,93,241,.45),transparent 48%),
      linear-gradient(145deg,rgba(12,42,112,.82),rgba(3,17,48,.88)) !important;
    border:1px solid rgba(142,181,255,.32) !important;
    box-shadow:inset 0 1px 0 rgba(243,247,254,.14),0 18px 42px rgba(0,0,0,.28),0 0 28px rgba(24,93,241,.13);
    -webkit-backdrop-filter:blur(24px);backdrop-filter:blur(24px);
  }
  .study-tabs {
    position:sticky;top:8px;z-index:30;padding:8px;border-radius:18px;
    background:rgba(3,17,48,.64);border:1px solid rgba(142,181,255,.22);
    -webkit-backdrop-filter:blur(28px) saturate(170%);backdrop-filter:blur(28px) saturate(170%);
    box-shadow:0 10px 30px rgba(0,0,0,.24),inset 0 1px 0 rgba(243,247,254,.10);
  }
  .study-tabs button {border:1px solid transparent;transition:all .22s ease;}
  .study-tabs button:not(.active):hover {background:rgba(24,93,241,.18);border-color:rgba(142,181,255,.22);color:#F3F7FE;}
  .mind-node {background:linear-gradient(145deg,rgba(24,93,241,.28),rgba(3,17,48,.66)) !important;border-color:rgba(142,181,255,.30) !important;box-shadow:inset 0 1px 0 rgba(243,247,254,.12);}
  .chapter-num {background:linear-gradient(145deg,#185DF1,#0A2B70) !important;color:#fff !important;border-color:rgba(190,211,255,.35) !important;}
  .tag-pill,.studio-pill {background:rgba(24,93,241,.17) !important;border:1px solid rgba(142,181,255,.24) !important;color:#DCE8FF !important;}
  .typing-dot,.status-dot.connected {background:#75A4FF !important;box-shadow:0 0 12px rgba(24,93,241,.7) !important;}
  .gradient-text,.text-blue-300,.text-cyan-300 {color:#BBD1FF;}
  a:focus-visible,button:focus-visible {outline:2px solid #8EB5FF;outline-offset:3px;border-radius:12px;}
  @media (max-width:640px) {
    .study-tabs {overflow-x:auto;scrollbar-width:none;}
    .study-tabs::-webkit-scrollbar {display:none;}
  }
  @media (prefers-reduced-motion:reduce) { .study-tabs button,.card-base,.chapter-link {transition:none !important;} }

</style>
"""


# 
# BOTTOM NAV
# 

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
        wrap_style = ("background:linear-gradient(135deg,rgba(145,215,255,0.15),rgba(105,177,225,0.15));"
                      "border-color:rgba(145,215,255,0.35);") if is_active else \
                     ("background:rgba(255,255,255,0.04);border-color:rgba(255,255,255,0.08);")
        stroke_color = "#A9DFFF" if is_active else "#9A9B98"
        text_class = 'text-[9px] font-semibold' if is_active else 'text-[9px] font-medium text-muted'
        text_style = 'color:#A9DFFF;' if is_active else ''
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
        <a href="/learn" class="nav-icon flex flex-col items-center gap-0.5 flex-1"><div class="icon-wrap w-9 h-9 rounded-2xl flex items-center justify-center border border-transparent" style="background:linear-gradient(135deg,#A9DFFF,#397DA8);border-color:rgba(139,92,246,.3);color:#fff;font-size:9px;font-weight:900;letter-spacing:-.5px;">ST</div><span class="text-[9px] font-semibold" style="color:#C9EAFF;">Learn</span></a>
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


# 
# TEMPLATES
# 

ONBOARDING_TEMPLATE = """
<!DOCTYPE html><html lang="en"><head>
<title>Study Tracker — Setup</title>
""" + BASE_STYLE + """
</head><body class="flex items-center justify-center min-h-screen p-4">
  <div class="w-full max-w-md fade-in">
    <div class="absolute top-0 left-1/2 -translate-x-1/2 w-96 h-96 rounded-full opacity-10 pointer-events-none" style="background:radial-gradient(circle,#A9DFFF 0%,transparent 70%);filter:blur(60px);"></div>
    <div class="card-base p-8 relative overflow-hidden" style="box-shadow:0 25px 60px rgba(0,0,0,0.5);">
      <div class="absolute -top-10 -right-10 w-40 h-40 rounded-full opacity-20" style="background:linear-gradient(135deg,#A9DFFF,#75BFEA);filter:blur(40px);"></div>
      <div class="absolute -bottom-10 -left-10 w-32 h-32 rounded-full opacity-15" style="background:linear-gradient(135deg,#75BFEA,#A9DFFF);filter:blur(40px);"></div>
      <div class="relative z-10">
        <div class="flex items-center gap-3 mb-8">
          <div class="w-12 h-12 rounded-2xl flex items-center justify-center text-2xl" style="background:linear-gradient(135deg,#A9DFFF22,#75BFEA22);border:1px solid rgba(145,215,255,0.3);"></div>
          <div>
            <div class="gradient-text font-black text-xl tracking-tight">StudyTracker</div>
            <div class="text-xs text-muted">Class 10 Academic Hub</div>
          </div>
        </div>
        <h1 class="text-3xl font-black text-white mb-2 leading-tight">Set Up Your<br/><span class="gradient-text">Command Center</span></h1>
        <p class="text-muted text-sm mb-8">Join {{ total_users }} student{{ 's' if total_users != 1 else '' }} on their learning journey.</p>
        {% if error %}
        <div class="mb-4 p-3 rounded-xl text-sm" style="background:rgba(255,100,100,0.1);border:1px solid rgba(255,100,100,0.3);color:#82BCE0;"> {{ error }}</div>
        {% endif %}
        <form method="POST" action="/setup" class="space-y-4">
          <div>
            <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Your Name</label>
            <input type="text" name="name" placeholder="e.g. Arjun Sharma" required maxlength="100" class="input-field w-full px-4 py-3.5 text-sm font-medium placeholder-gray-600"/>
          </div>
          <div>
            <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Your Class</label>
            <select name="student_class" required class="input-field w-full px-4 py-3.5 text-sm font-medium"><option value="Class 9">Class 9</option><option value="Class 10" selected>Class 10</option></select>
          </div>
          <button type="submit" class="gradient-btn w-full py-4 rounded-xl text-white font-bold text-sm tracking-wide mt-2"> Launch My Dashboard</button>
        </form>
        <div class="flex items-center justify-center gap-2 mt-6 text-xs text-muted">
          <span class="inline-block w-2 h-2 rounded-full online-pulse" style="background:#9CCFEA;"></span>
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
  <div class="fixed pointer-events-none" style="top:-100px;left:-100px;width:400px;height:400px;background:radial-gradient(circle,rgba(145,215,255,0.06) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>
  <div class="fixed pointer-events-none" style="bottom:-100px;right:-100px;width:500px;height:500px;background:radial-gradient(circle,rgba(105,177,225,0.06) 0%,transparent 70%);filter:blur(80px);z-index:0;"></div>

  <div class="relative z-10 scroll-content">
    <div class="max-w-2xl mx-auto px-4 pt-6 pb-4">

      <div class="flex items-center justify-between mb-4 fade-in">
        <a href="/chat" class="flex items-center gap-2 px-3 py-1.5 rounded-full" style="background:rgba(0,242,96,0.08);border:1px solid rgba(0,242,96,0.25);">
          <span class="inline-block w-2 h-2 rounded-full online-pulse" style="background:#9CCFEA;"></span>
          <span class="text-xs font-bold js-online-count" style="color:#9CCFEA;">{{ online_count }}</span>
          <span class="text-xs text-muted">live now</span>
        </a>
        <div class="text-xs text-muted">{{ total_users }} total learner{{ 's' if total_users != 1 else '' }}</div>
      </div>

      <div class="flex items-center justify-between mb-6 fade-in">
        <div>
          <p class="text-xs font-semibold text-muted uppercase tracking-widest mb-1">Welcome back </p>
          <h1 class="text-2xl font-black text-white leading-tight">{{ user.name }}</h1>
          <div class="flex items-center gap-2 mt-1">
            <span class="text-xs px-2 py-0.5 rounded-full font-semibold" style="background:rgba(145,215,255,0.12);color:#A9DFFF;border:1px solid rgba(145,215,255,0.2);">{{ user.student_class }}</span>
            <span class="text-xs text-muted">· Academic Hub</span>
          </div>
        </div>
        <div class="avatar" style="background:linear-gradient(145deg,rgba(169,223,255,.24),rgba(80,135,170,.12));border:1px solid rgba(169,223,255,.35);box-shadow:inset 0 1px 0 rgba(255,255,255,.16);color:#e9e4dc;width:44px;height:44px;font-size:14px;">{{ user.name | initials }}</div>
      </div>

      {% if nearest_exam %}
      <a href="/exam-zone" class="card-base card-glow p-4 mb-4 fade-in block" style="animation-delay:0.05s;background:linear-gradient(135deg,rgba(120,190,235,0.08),rgba(105,177,225,0.08));border-color:rgba(120,190,235,0.2);">
        <div class="flex items-center gap-3">
          <div class="text-3xl"></div>
          <div class="flex-1">
            <div class="text-xs text-muted font-semibold uppercase tracking-wider">Next Exam</div>
            <div class="text-sm font-bold text-white">{{ nearest_exam.subject }} · <span style="color:#82BCE0;">{{ nearest_exam.days_left }} day{{ 's' if nearest_exam.days_left != 1 else '' }} left</span></div>
            <div class="text-xs text-muted">Study <span class="font-bold" style="color:#A9DFFF;">{{ nearest_exam.per_day_units }} units/day</span> to finish in time →</div>
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
              <defs><linearGradient id="ringGrad" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" style="stop-color:#A9DFFF"/><stop offset="100%" style="stop-color:#75BFEA"/></linearGradient></defs>
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
              <div class="h-2 rounded-full" style="width:{{ stats.overall_pct }}%;background:linear-gradient(90deg,#A9DFFF,#75BFEA);transition:width 0.8s ease;"></div>
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
              <span class="text-xs font-bold mb-1" style="color:{% if day.is_today %}#A9DFFF{% else %}#E7F5FF{% endif %};font-size:10px;">{{ day.count }}</span>
              {% endif %}
              <div class="bar-chart-bar w-full rounded-t-lg" style="
                height:{{ bar_height if bar_height >= 6 else (6 if day.count > 0 else 3) }}%;
                background:{% if day.is_today %}linear-gradient(180deg,#A9DFFF,#75BFEA){% elif day.count > 0 %}linear-gradient(180deg,rgba(145,215,255,0.6),rgba(105,177,225,0.4)){% else %}rgba(255,255,255,0.06){% endif %};
                min-height:4px;
                box-shadow:{% if day.is_today %}0 0 12px rgba(145,215,255,0.4){% else %}none{% endif %};
              "></div>
            </div>
            <div class="text-center">
              <div class="text-xs font-semibold" style="color:{% if day.is_today %}#A9DFFF{% else %}#9A9B98{% endif %};font-size:10px;">{{ day.day_label }}</div>
              <div class="text-muted" style="font-size:9px;">{{ day.date }}</div>
            </div>
          </div>
          {% endfor %}
        </div>
      </div>

      <div class="grid grid-cols-3 gap-3 mb-4 fade-in" style="animation-delay:0.15s;">
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1" aria-hidden="true" style="color:#A9DFFF;font-weight:300;">—</div>
          <div class="text-xl font-black text-white">{{ user.streak or 0 }}</div>
          <div class="text-xs text-muted leading-tight mt-0.5">Day<br/>Streak</div>
        </div>
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1" aria-hidden="true" style="color:#A9DFFF;font-weight:300;">—</div>
          <div class="text-xl font-black gradient-text">{{ stats.active_chambers }}/{{ stats.total_subjects }}</div>
          <div class="text-xs text-muted leading-tight mt-0.5">Active<br/>Chambers</div>
        </div>
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1" aria-hidden="true" style="color:#A9DFFF;font-weight:300;">—</div>
          <div class="text-xl font-black" style="color:#9CCFEA;">{{ stats.syllabus_pct }}%</div>
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
                {% if info.is_custom %}<span class="text-xs" style="color:#9CCFEA;"></span>{% endif %}
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
          <button onclick="openAddSubject()" class="subject-card card-base block text-left" style="border:2px dashed rgba(145,215,255,0.25);background:rgba(145,215,255,0.02);">
            <div class="p-5 flex flex-col items-center justify-center h-full min-h-[160px]">
              <div class="w-11 h-11 rounded-xl flex items-center justify-center text-2xl mb-3" style="background:linear-gradient(135deg,#A9DFFF22,#75BFEA22);border:1px solid rgba(145,215,255,0.3);">+</div>
              <h3 class="text-sm font-bold text-white text-center">Add Subject</h3>
              <p class="text-xs text-muted mt-1 text-center">Create custom chamber</p>
            </div>
          </button>
        </div>
      </div>

      <a href="/chat" class="mt-4 fade-in block card-base card-glow" style="animation-delay:0.25s;border-radius:16px;background:linear-gradient(135deg,rgba(145,215,255,0.08),rgba(105,177,225,0.08));border:1px solid rgba(145,215,255,0.15);padding:16px 20px;">
        <div class="flex items-center gap-3">
          <span class="text-2xl"></span>
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
        <button onclick="closeAddSubject()" class="w-8 h-8 rounded-lg flex items-center justify-center" style="background:rgba(255,255,255,0.05);"></button>
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
        <button type="submit" class="gradient-btn w-full py-3 rounded-xl text-white font-bold text-sm mt-2"> Create Subject</button>
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
    el.style.background = 'linear-gradient(135deg,#A9DFFF,#75BFEA)';
    el.style.borderColor = 'transparent';
    el.style.boxShadow = '0 0 12px rgba(145,215,255,0.5)';
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
    if (pctLabel) { pctLabel.textContent = ' Done'; pctLabel.style.color = '#9CCFEA'; }
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
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#9A9B98" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
        </a>
        <div class="flex-1">
          <div class="flex items-center gap-2">
            <span class="text-2xl">{{ meta.icon }}</span>
            <h1 class="text-xl font-black text-white">{{ subject }}</h1>
            {% if info.is_custom %}<span class="text-xs px-2 py-0.5 rounded-full font-bold" style="background:rgba(0,242,96,0.12);color:#9CCFEA;border:1px solid rgba(0,242,96,0.25);">CUSTOM</span>{% endif %}
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
      <a href="/exam-zone" class="card-base block p-3 mb-4 fade-in" style="animation-delay:0.11s;background:linear-gradient(135deg,rgba(120,190,235,0.08),{{ meta.color_from }}0D);border-color:rgba(120,190,235,0.2);">
        <div class="flex items-center gap-3">
          <div class="text-2xl"></div>
          <div class="flex-1">
            <div class="text-xs text-muted font-semibold uppercase tracking-wider">Exam · {{ exam_plan.exam_date_str }}</div>
            <div class="text-sm font-bold text-white">
              {% if exam_plan.days_left < 0 %}<span style="color:#82BCE0;">Exam passed</span>
              {% elif exam_plan.days_left == 0 %}<span style="color:#82BCE0;">Today is exam day! </span>
              {% else %}<span style="color:#82BCE0;">{{ exam_plan.days_left }} day{{ 's' if exam_plan.days_left != 1 else '' }} left</span> · Study <span style="color:#A9DFFF;">{{ exam_plan.per_day_units }} unit{{ 's' if exam_plan.per_day_units != 1 else '' }}/day</span>
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
        <button onclick="deleteSubject()" class="btn-secondary btn-danger flex items-center justify-center gap-1.5 px-4" title="Delete subject"></button>
        {% endif %}
      </div>

      {% if not info.is_custom %}
      <p class="text-xs text-muted mb-3 px-1"> Default chapters can't be removed, but you can add your own extras below.</p>
      {% endif %}

      {% if chapters | length == 0 %}
      <div class="card-base p-8 text-center fade-in">
        <div class="text-4xl mb-3"></div>
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
              <a class="text-sm font-semibold text-white leading-snug hover:underline" href="/study/{{ '9' if user.student_class and ('9' in user.student_class or 'ix' in user.student_class.lower()) else '10' }}/{{ subject | urlencode }}/{{ ci }}">{{ chapter }}</a>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              <span class="chapter-pct text-xs font-bold" style="color:{% if ch_pct == 100 %}#9CCFEA{% else %}{{ meta.color_from }}{% endif %};">
                {% if ch_pct == 100 %} Done{% else %}{{ ch_pct }}%{% endif %}
              </span>
              {% if info.is_custom %}
              <button onclick="deleteChapter({{ ci }}, {{ chapter | tojson }})" class="text-muted hover:text-red-400 text-xs" title="Delete chapter"></button>
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
                   style="{% if is_filled %}background:linear-gradient(135deg,#A9DFFF,#75BFEA);border-color:transparent;box-shadow:0 0 12px rgba(145,215,255,0.5);{% else %}background:rgba(255,255,255,0.04);border-color:rgba(255,255,255,0.12);{% endif %}">
                <span class="circle-check absolute text-white font-bold" style="font-size:14px;opacity:{% if is_filled %}1{% else %}0{% endif %};transition:opacity 0.2s;"></span>
                <span class="circle-label absolute text-center font-semibold" style="font-size:8px;color:#9A9B98;opacity:{% if is_filled %}0{% else %}1{% endif %};transition:opacity 0.2s;line-height:1.1;">{{ circle_labels[circle_i] }}</span>
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
        <button onclick="closeAddChapter()" class="w-8 h-8 rounded-lg flex items-center justify-center" style="background:rgba(255,255,255,0.05);"></button>
      </div>
      <form method="POST" action="/add_chapter" class="space-y-4">
        <input type="hidden" name="subject_name" value="{{ subject }}"/>
        <div>
          <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-2">Chapter / Topic Name</label>
          <input type="text" name="name" placeholder="e.g. Newton's Laws of Motion" required maxlength="200" class="input-field w-full px-4 py-3 text-sm font-medium" autofocus/>
        </div>
        <p class="text-xs text-muted"> Add topics, chapters, or study points. Each gets 5 progress trackers.</p>
        <button type="submit" class="gradient-btn w-full py-3 rounded-xl text-white font-bold text-sm"> Add Chapter</button>
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
  avatar.style.background = `linear-gradient(135deg, ${m.user_avatar_color || '#A9DFFF'}, #75BFEA)`;
  avatar.textContent = initials(m.user_name);

  const bubbleWrap = document.createElement('div');
  bubbleWrap.className = 'flex flex-col ' + (isMine ? 'items-end' : 'items-start');
  bubbleWrap.style.maxWidth = 'calc(100% - 50px)';

  const meta = document.createElement('div');
  meta.className = 'text-xs text-muted mb-1 px-1 flex items-center gap-1.5';
  meta.innerHTML = isMine
    ? `<span>${esc(m.time_ago)}</span><span class="font-semibold" style="color:#A9DFFF">You</span>`
    : `<span class="font-semibold text-white">${esc(m.user_name)}</span><span class="text-[10px] px-1.5 py-0.5 rounded-full" style="background:rgba(145,215,255,0.1);color:#A9DFFF;">${esc(m.user_class)}</span><span>${esc(m.time_ago)}</span>`;

  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + (isMine ? 'mine' : 'theirs');
  let replyHtml = '';
  if (m.reply_to) {
    replyHtml = `<div class="reply-preview" style="margin-bottom:6px;">
      <div><span style="color:#A9DFFF;font-weight:700;">↳ ${esc(m.reply_to.user_name)}:</span> <span style="color:#9A9B98;">${esc(m.reply_to.message.substring(0,60))}${m.reply_to.message.length > 60 ? '…' : ''}</span></div>
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
  preview.querySelector('.reply-text').innerHTML = `<span style="color:#A9DFFF;font-weight:700;">↳ ${esc(userName)}:</span> <span style="color:#9A9B98;">${esc(message.substring(0,60))}${message.length > 60 ? '…' : ''}</span>`;
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
function showToast(msg, color='#A9DFFF') {
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
        div.style.cssText = `width:26px;height:26px;font-size:9px;background:linear-gradient(135deg,${u.avatar_color},#75BFEA);border:2px solid #080F17;`;
        div.title = `${u.name} (${u.student_class})`;
        div.textContent = initials(u.name);
        strip.appendChild(div);
      });
      if (data.users.length > 8) {
        const more = document.createElement('div');
        more.className = 'avatar';
        more.style.cssText = 'width:26px;height:26px;font-size:9px;background:rgba(255,255,255,0.1);border:2px solid #080F17;color:#E7F5FF;';
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
      showToast(` <strong>${esc(data.name)}</strong> joined ${esc(data.room_name)}`, '#9CCFEA');
    }
  });
  socket.on('user_left', (data) => {
    if (data.user_id !== CURRENT_USER_ID) {
      showToast(` <strong>${esc(data.name)}</strong> left`, '#9A9B98');
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
  <div class="fixed pointer-events-none" style="top:-80px;right:-80px;width:350px;height:350px;background:radial-gradient(circle,rgba(145,215,255,0.06) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>

  <div class="relative z-10">
    <div class="max-w-2xl mx-auto">
      <div class="px-4 pt-6 pb-3">
        <div class="flex items-center gap-3 mb-4 fade-in">
          <a href="/" class="w-10 h-10 rounded-xl flex items-center justify-center" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#9A9B98" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
          </a>
          <div class="flex-1">
            <div class="flex items-center gap-2">
              <span class="text-2xl"></span>
              <h1 class="text-xl font-black text-white">Live Chat</h1>
              <div class="flex items-center gap-1.5 ml-1">
                <span id="connStatus" class="status-dot connecting"></span>
                <span id="connLabel" class="text-xs text-muted">Connecting...</span>
              </div>
            </div>
            <div class="flex items-center gap-2 mt-0.5 text-xs">
              <span class="inline-block w-2 h-2 rounded-full online-pulse" style="background:#9CCFEA;"></span>
              <span style="color:#9CCFEA;"><span class="js-online-count">{{ online_count }}</span> online</span>
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
               style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#9A9B98;">
              <span>{{ room.icon }}</span><span>{{ room.name }}</span>
            </a>
            {% endfor %}
          </div>
        </div>

        <div class="flex items-center gap-2 mt-3 fade-in" style="animation-delay:0.08s;">
          <span class="text-xs text-muted flex-shrink-0"> Online:</span>
          <div id="onlineUsersStrip" class="flex -space-x-2">
            {% for ou in online_users[:8] %}
            <div class="avatar" style="width:26px;height:26px;font-size:9px;background:linear-gradient(135deg,{{ ou.avatar_color }},#75BFEA);border:2px solid #080F17;" title="{{ ou.name }} ({{ ou.student_class }})">
              {{ ou.name | initials }}
            </div>
            {% endfor %}
            {% if online_users | length > 8 %}
            <div class="avatar" style="width:26px;height:26px;font-size:9px;background:rgba(255,255,255,0.1);border:2px solid #080F17;color:#E7F5FF;">+{{ online_users|length - 8 }}</div>
            {% endif %}
          </div>
        </div>
      </div>

      <div id="chatScroll" class="chat-scroll">
        {% if messages | length == 0 %}
        <div class="flex flex-col items-center justify-center h-full text-center px-6" id="emptyState">
          <div class="text-5xl mb-3"></div>
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
        <button onclick="cancelReply()" class="text-muted hover:text-white ml-2 flex-shrink-0" style="font-size:16px;"></button>
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
  <div class="fixed pointer-events-none" style="top:-100px;right:-100px;width:400px;height:400px;background:radial-gradient(circle,rgba(120,190,235,0.08) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>
  <div class="relative z-10 scroll-content">
    <div class="max-w-2xl mx-auto px-4 pt-8 pb-4">
      <div class="flex items-center gap-4 mb-6 fade-in">
        <a href="/" class="w-10 h-10 rounded-xl flex items-center justify-center" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#9A9B98" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
        </a>
        <div class="flex-1">
          <div class="flex items-center gap-2">
            <span class="text-2xl"></span>
            <h1 class="text-2xl font-black text-white">Exam Zone</h1>
          </div>
          <p class="text-xs text-muted">Plan your syllabus around exam dates</p>
        </div>
      </div>

      <div class="grid grid-cols-3 gap-3 mb-4 fade-in" style="animation-delay:0.08s;">
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1" aria-hidden="true" style="color:#A9DFFF;font-weight:300;">—</div>
          <div class="text-xl font-black text-white">{{ exam_plans | length }}</div>
          <div class="text-xs text-muted leading-tight mt-0.5">Exams<br/>Scheduled</div>
        </div>
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1" aria-hidden="true" style="color:#A9DFFF;font-weight:300;">—</div>
          <div class="text-xl font-black" style="color:#82BCE0;">
            {% if nearest_days is not none %}{{ nearest_days }}{% else %}—{% endif %}
          </div>
          <div class="text-xs text-muted leading-tight mt-0.5">Nearest<br/>Exam Day{{ 's' if nearest_days != 1 else '' }}</div>
        </div>
        <div class="card-base p-4 flex flex-col items-center text-center" style="border-radius:14px;">
          <div class="text-2xl mb-1" aria-hidden="true" style="color:#A9DFFF;font-weight:300;">—</div>
          <div class="text-xl font-black gradient-text">{{ today_target }}</div>
          <div class="text-xs text-muted leading-tight mt-0.5">Units<br/>Today</div>
        </div>
      </div>

      <button onclick="openScheduleModal()" class="gradient-btn w-full py-3.5 rounded-xl text-white font-bold text-sm mb-5 fade-in" style="animation-delay:0.1s;">
        + Schedule New Exam
      </button>

      {% if exam_plans | length == 0 %}
      <div class="card-base p-8 text-center fade-in" style="animation-delay:0.15s;">
        <div class="text-5xl mb-3"></div>
        <h3 class="text-lg font-black text-white mb-1">No Exams Scheduled</h3>
        <p class="text-sm text-muted mb-4 max-w-xs mx-auto">Add your first exam date and get a personalized daily study target.</p>
        <button onclick="openScheduleModal()" class="gradient-btn px-6 py-3 rounded-xl text-white font-bold text-sm"> Schedule First Exam</button>
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
                <div class="text-xs text-muted mt-0.5"> {{ plan.exam_day }}, {{ plan.exam_date_str }}</div>
              </div>
            </div>
            <div class="text-right flex-shrink-0">
              {% if plan.status == 'past' %}
                <span class="countdown-badge" style="background:rgba(150,150,150,0.15);color:#9A9B98;">PAST</span>
              {% elif plan.status == 'today' %}
                <span class="countdown-badge" style="background:rgba(255,60,60,0.15);color:#82BCE0;">TODAY!</span>
              {% else %}
                <span class="countdown-badge" style="background:{% if plan.days_left <= 7 %}rgba(255,60,60,0.15);color:#82BCE0{% elif plan.days_left <= 21 %}rgba(255,180,50,0.15);color:#B7E5FF{% else %}rgba(0,242,96,0.15);color:#9CCFEA{% endif %};">
                   {{ plan.days_left }} DAY{{ 'S' if plan.days_left != 1 else '' }}
                </span>
              {% endif %}
              <button onclick="deleteExam({{ plan.id }})" class="text-xs text-muted mt-1 opacity-60 hover:opacity-100 block ml-auto" title="Delete"></button>
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
            <div class="text-2xl"></div>
            <div>
              <div class="text-sm font-bold" style="color:#9CCFEA;">Syllabus Complete!</div>
              <div class="text-xs text-muted">You're 100% ready. Just revise now.</div>
            </div>
          </div>
          {% elif plan.status == 'past' %}
          <div class="rounded-xl p-3 text-center" style="background:rgba(150,150,150,0.06);">
            <div class="text-xs text-muted">Exam date has passed</div>
          </div>
          {% else %}
          <div class="grid grid-cols-3 gap-2 mb-3">
            <div class="rounded-xl p-3 text-center" style="background:rgba(145,215,255,0.08);border:1px solid rgba(145,215,255,0.2);">
              <div class="text-xl font-black" style="color:#A9DFFF;">{{ plan.per_day_units }}</div>
              <div class="text-muted leading-tight mt-0.5" style="font-size:10px;">Units<br/>per day</div>
            </div>
            <div class="rounded-xl p-3 text-center" style="background:rgba(105,177,225,0.08);border:1px solid rgba(105,177,225,0.2);">
              <div class="text-xl font-black" style="color:#75BFEA;">{{ plan.per_day_chapters }}</div>
              <div class="text-muted leading-tight mt-0.5" style="font-size:10px;">Chapters<br/>per day</div>
            </div>
            <div class="rounded-xl p-3 text-center" style="background:rgba(0,242,96,0.08);border:1px solid rgba(0,242,96,0.2);">
              <div class="text-xl font-black" style="color:#9CCFEA;">{{ plan.per_week_chapters }}</div>
              <div class="text-muted leading-tight mt-0.5" style="font-size:10px;">Chapters<br/>per week</div>
            </div>
          </div>
          <div class="flex items-center justify-between text-xs text-muted pt-2 border-t" style="border-color:rgba(255,255,255,0.05);">
            <span>{{ plan.remaining_chapters }}/{{ plan.total_chapters }} chapters left</span>
            <span>{{ plan.remaining_units }}/{{ plan.total_units }} units left</span>
          </div>
          <div class="mt-3">
            {% if plan.feasibility == 'easy' %}
            <div class="text-xs font-semibold px-3 py-2 rounded-lg" style="background:rgba(0,242,96,0.1);color:#9CCFEA;border:1px solid rgba(0,242,96,0.25);"> Easy pace — you're in great shape!</div>
            {% elif plan.feasibility == 'moderate' %}
            <div class="text-xs font-semibold px-3 py-2 rounded-lg" style="background:rgba(145,215,255,0.1);color:#A9DFFF;border:1px solid rgba(145,215,255,0.25);"> Moderate pace — stay consistent</div>
            {% elif plan.feasibility == 'tough' %}
            <div class="text-xs font-semibold px-3 py-2 rounded-lg" style="background:rgba(255,180,50,0.1);color:#B7E5FF;border:1px solid rgba(255,180,50,0.3);"> Tough pace — buckle down!</div>
            {% elif plan.feasibility == 'urgent' %}
            <div class="text-xs font-semibold px-3 py-2 rounded-lg" style="background:rgba(255,60,60,0.1);color:#82BCE0;border:1px solid rgba(255,60,60,0.3);"> Urgent! Consider prioritizing key topics</div>
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
        <h2 class="text-lg font-black text-white">Schedule Exam </h2>
        <button onclick="closeScheduleModal()" class="w-8 h-8 rounded-lg flex items-center justify-center" style="background:rgba(255,255,255,0.05);"></button>
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
        <p class="text-xs text-muted"> Your daily study target will be calculated automatically.</p>
        <button type="submit" class="gradient-btn w-full py-3 rounded-xl text-white font-bold text-sm"> Schedule Exam</button>
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
  <div class="fixed pointer-events-none" style="top:-80px;left:-80px;width:400px;height:400px;background:radial-gradient(circle,rgba(145,215,255,0.06) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>
  <div class="relative z-10 scroll-content">
    <div class="max-w-2xl mx-auto px-4 pt-8 pb-4">
      <div class="flex items-center gap-4 mb-6 fade-in">
        <a href="/" class="w-10 h-10 rounded-xl flex items-center justify-center" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#9A9B98" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
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
          <div class="w-9 h-9 rounded-xl flex items-center justify-center" style="background:linear-gradient(135deg,#A9DFFF22,#75BFEA22);border:1px solid rgba(145,215,255,0.25);"></div>
        </div>
        <div class="flex items-end justify-between gap-2" style="height:150px;">
          {% for day in week_data %}
          {% set bar_height = (day.count / week_max * 100) if week_max > 0 else 0 %}
          <div class="flex flex-col items-center flex-1 gap-1.5">
            <div class="w-full flex flex-col justify-end items-center" style="height:115px;">
              {% if day.count > 0 %}
              <span class="text-xs font-bold mb-1" style="color:{% if day.is_today %}#A9DFFF{% else %}#E7F5FF{% endif %};">{{ day.count }}</span>
              {% endif %}
              <div class="bar-chart-bar w-full rounded-t-lg" style="
                height:{{ bar_height if bar_height >= 6 else (6 if day.count > 0 else 3) }}%;
                background:{% if day.is_today %}linear-gradient(180deg,#A9DFFF,#75BFEA){% elif day.count > 0 %}linear-gradient(180deg,rgba(145,215,255,0.6),rgba(105,177,225,0.4)){% else %}rgba(255,255,255,0.06){% endif %};
                min-height:4px;
                box-shadow:{% if day.is_today %}0 0 12px rgba(145,215,255,0.4){% else %}none{% endif %};
              "></div>
            </div>
            <div class="text-center">
              <div class="text-xs font-semibold" style="color:{% if day.is_today %}#A9DFFF{% else %}#9A9B98{% endif %};">{{ day.day_label }}</div>
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
              <defs><linearGradient id="ag" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" style="stop-color:#A9DFFF"/><stop offset="100%" style="stop-color:#75BFEA"/></linearGradient></defs>
            </svg>
            <div class="absolute inset-0 flex flex-col items-center justify-center">
              <span class="text-lg font-black text-white">{{ stats.overall_pct }}%</span>
            </div>
          </div>
          <div class="flex-1 space-y-2">
            <div class="flex justify-between text-sm"><span class="text-muted">Completed Units</span><span class="font-bold text-white">{{ stats.completed_circles }}</span></div>
            <div class="flex justify-between text-sm"><span class="text-muted">Total Units</span><span class="font-bold text-white">{{ stats.total_circles }}</span></div>
            <div class="flex justify-between text-sm"><span class="text-muted">Study Streak</span><span class="font-bold" style="color:#82BCE0;">{{ user.streak or 0 }} </span></div>
            <div class="flex justify-between text-sm"><span class="text-muted">Chapters Done</span><span class="font-bold" style="color:#9CCFEA;">{{ stats.fully_done_chapters }}/{{ stats.total_chapters }}</span></div>
          </div>
        </div>
      </div>

      <div class="card-base p-5 mb-4 fade-in" style="animation-delay:0.12s;background:linear-gradient(135deg,rgba(0,242,96,0.05),rgba(145,215,255,0.05));border-color:rgba(0,242,96,0.15);">
        <h2 class="text-sm font-bold text-white mb-3 flex items-center gap-2"> Community Pulse (Live)</h2>
        <div class="grid grid-cols-2 gap-3">
          <div class="rounded-xl p-3 text-center" style="background:rgba(0,242,96,0.05);border:1px solid rgba(0,242,96,0.15);">
            <div class="flex items-center justify-center gap-1">
              <span class="inline-block w-2 h-2 rounded-full online-pulse" style="background:#9CCFEA;"></span>
              <div class="text-2xl font-black js-online-count" style="color:#9CCFEA;">{{ online_count }}</div>
            </div>
            <div class="text-xs text-muted mt-1">Online Now</div>
          </div>
          <div class="rounded-xl p-3 text-center" style="background:rgba(145,215,255,0.05);border:1px solid rgba(145,215,255,0.15);">
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
            <div class="flex-1 h-1 rounded-full" style="background:{% if ch_done == 5 %}#9CCFEA{% elif ch_done > 0 %}{{ meta.color_from }}{% else %}rgba(255,255,255,0.08){% endif %};opacity:0.9;"></div>
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
  <div class="fixed pointer-events-none" style="top:-80px;right:-80px;width:400px;height:400px;background:radial-gradient(circle,rgba(105,177,225,0.07) 0%,transparent 70%);filter:blur(60px);z-index:0;"></div>
  <div class="relative z-10 scroll-content">
    <div class="max-w-2xl mx-auto px-4 pt-8 pb-4">
      <div class="flex items-center gap-4 mb-6 fade-in">
        <a href="/" class="w-10 h-10 rounded-xl flex items-center justify-center" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#9A9B98" stroke-width="2.5" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>
        </a>
        <div>
          <h1 class="text-2xl font-black text-white">Settings</h1>
          <p class="text-xs text-muted">Manage your profile & data</p>
        </div>
      </div>

      <div class="card-base p-6 mb-4 fade-in" style="animation-delay:0.1s;">
        <div class="flex items-center gap-4 mb-4">
          <div class="avatar" style="width:56px;height:56px;font-size:18px;background:linear-gradient(135deg,{{ user.avatar_color }},#75BFEA);">{{ user.name | initials }}</div>
          <div>
            <h2 class="text-lg font-black text-white">{{ user.name }}</h2>
            <p class="text-sm text-muted">{{ user.student_class }}</p>
            <p class="text-xs text-muted mt-0.5">Member since {{ member_since }}</p>
          </div>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div class="rounded-xl p-3 text-center" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.07);">
            <div class="text-2xl font-black text-white">{{ user.streak or 0 }}</div>
            <div class="text-xs text-muted">Day Streak </div>
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
          <button type="submit" name="color" value="{{ c }}" class="w-10 h-10 rounded-full transition-all {% if user.avatar_color == c %}ring-4 ring-white{% endif %}" style="background:linear-gradient(135deg,{{ c }},#75BFEA);"></button>
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
        <div class="mb-3 p-3 rounded-xl text-sm" style="background:rgba(0,242,96,0.1);border:1px solid rgba(0,242,96,0.3);color:#9CCFEA;"> {{ msg }}</div>
        {% endif %}
        <form method="POST" action="/settings" class="space-y-3">
          <div>
            <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-1.5">Name</label>
            <input type="text" name="name" value="{{ user.name }}" required maxlength="100" class="input-field w-full px-4 py-3 text-sm font-medium"/>
          </div>
          <div>
            <label class="block text-xs font-semibold text-muted uppercase tracking-widest mb-1.5">Class</label>
            <select name="student_class" required class="input-field w-full px-4 py-3 text-sm font-medium"><option value="Class 9" {% if '9' in user.student_class or 'ix' in user.student_class|lower %}selected{% endif %}>Class 9</option><option value="Class 10" {% if not ('9' in user.student_class or 'ix' in user.student_class|lower) %}selected{% endif %}>Class 10</option></select>
          </div>
          <button type="submit" class="gradient-btn w-full py-3 rounded-xl text-white font-bold text-sm">Save Changes</button>
        </form>
      </div>

      <div class="card-base p-5 fade-in" style="animation-delay:0.2s;border-color:rgba(255,100,100,0.15);">
        <h3 class="text-sm font-bold mb-1" style="color:#82BCE0;">Danger Zone</h3>
        <p class="text-xs text-muted mb-4">This will permanently delete all your data.</p>
        <form method="POST" action="/reset" onsubmit="return confirm('Are you sure? All data will be deleted permanently.');">
          <button type="submit" class="w-full py-3 rounded-xl text-sm font-bold border transition-all" style="background:rgba(255,100,100,0.08);border-color:rgba(255,100,100,0.3);color:#82BCE0;"> Reset All Data & Start Over</button>
        </form>
      </div>
    </div>
  </div>
  """ + presence_script() + """
  {{ nav_html | safe }}
</body></html>
"""



# 
# CBSE LEARNING STUDIO — GRADE-SCOPED NOTES, MIND MAPS & PRACTICE
# 

LEARNING_STUDIO_TEMPLATE = r"""
<!DOCTYPE html><html lang="{{ 'hi' if language == 'hi' else 'en' }}"><head>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{{ 'अध्ययन केंद्र' if language == 'hi' else 'Learning Studio' }} — StudyTracker</title>
""" + BASE_STYLE + r"""
<style>
.studio-wrap{max-width:1040px;margin:0 auto;padding:28px 18px 112px}
.studio-hero{background:radial-gradient(ellipse at 85% 10%,rgba(139,92,246,.34),transparent 45%),linear-gradient(135deg,#17132c,#0c0d18 70%);border:1px solid rgba(177,140,255,.22);border-radius:26px;padding:26px;box-shadow:0 20px 65px rgba(0,0,0,.28)}
.studio-card{background:linear-gradient(145deg,rgba(30,27,48,.92),rgba(16,17,29,.96));border:1px solid rgba(180,160,255,.13);border-radius:20px;transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease;content-visibility:auto;contain-intrinsic-size:180px}
.studio-card:active{transform:scale(.985)}
.studio-card:hover{border-color:rgba(177,140,255,.42);box-shadow:0 14px 34px rgba(64,40,130,.16)}
.grade-switch{display:flex;gap:8px;background:rgba(255,255,255,.05);padding:5px;border-radius:14px}
.grade-switch a{padding:10px 16px;border-radius:10px;color:#a9a4bd;font-size:13px;font-weight:800}
.grade-switch a.active{background:linear-gradient(135deg,#A9DFFF,#477FA3);color:white;box-shadow:0 5px 18px rgba(113,80,220,.28)}
.studio-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}
.studio-pill{display:inline-flex;border:1px solid rgba(177,140,255,.2);background:rgba(139,92,246,.1);color:#c9b8ff;border-radius:999px;padding:5px 10px;font-size:11px;font-weight:700}
@media(prefers-reduced-motion:no-preference){.studio-card{will-change:transform}.studio-card:hover{transform:translateY(-3px)}}
</style>
</head><body>
<div class="studio-wrap">
  <div class="studio-hero mb-6">
    <div class="flex items-center justify-between gap-3 flex-wrap">
      <span class="studio-pill">CBSE · {{ session_label }}</span>
      <div class="grade-switch">
        <a href="/learn?grade=9" class="{{ 'active' if grade == '9' else '' }}">Class 9</a>
        <a href="/learn?grade=10" class="{{ 'active' if grade == '10' else '' }}">Class 10</a>
      </div>
    </div>
    <p class="text-xs uppercase tracking-widest mt-6" style="color:#C9EAFF;font-weight:800;">{{ 'आपका अध्ययन कार्यक्षेत्र' if language == 'hi' else 'YOUR STUDY WORKSPACE' }}</p>
    <h1 class="text-3xl font-black text-white mt-2">{{ 'कक्षा' if language == 'hi' else 'Class' }} {{ grade }} · {{ 'सीखने की लाइब्रेरी' if language == 'hi' else 'Learning Library' }}</h1>
    <p class="text-sm mt-2" style="color:#A9C9DE;">{{ 'अध्यायवार नोट्स, माइंड मैप और अभ्यास' if language == 'hi' else 'Chapter-wise notes, mind maps and focused practice' }}</p>
    <div class="flex gap-2 flex-wrap mt-5">
      <span class="studio-pill">{{ total_chapters }} {{ 'अध्याय / अध्ययन इकाइयाँ' if language == 'hi' else 'chapters / study units' }}</span>
      <span class="studio-pill">{{ 'डिफ़ॉल्ट भाषा' if language == 'hi' else 'Default language' }}: {{ 'हिन्दी' if language == 'hi' else 'English' }}</span>
    </div>
  </div>
  <div class="flex items-center justify-between mb-4">
    <div><h2 class="text-lg font-black text-white">{{ 'विषय चुनें' if language == 'hi' else 'Choose a subject' }}</h2>
    <p class="text-xs text-muted">{{ 'आपकी कक्षा के पाठ्यक्रम के अनुसार' if language == 'hi' else 'Showing the selected class curriculum' }}</p></div>
    <a href="/" class="btn-secondary text-xs">← {{ 'डैशबोर्ड' if language == 'hi' else 'Dashboard' }}</a>
  </div>
  <div class="studio-grid">
    {% for subject, info in curriculum.items() %}
    <a class="studio-card p-5 block" href="{{ url_for('learning_subject', grade=grade, subject=subject) }}">
      <div class="flex items-start justify-between">
        <div class="w-12 h-12 rounded-2xl flex items-center justify-center text-lg font-black tracking-tight" style="background:linear-gradient(145deg,{{ info.meta.color_from }}30,{{ info.meta.color_to }}24);border:1px solid {{ info.meta.color_from }}55;color:#f2edff;box-shadow:inset 0 1px 0 rgba(255,255,255,.15);">{{ info.meta.icon }}</div>
        <span class="studio-pill">{{ info.chapters|length }} {{ 'अध्याय' if language == 'hi' else 'chapters' }}</span>
      </div>
      <h3 class="text-base font-black text-white mt-4">{{ subject }}</h3>
      <p class="text-xs text-muted mt-1">{{ info.meta.tag }}</p>
      <div class="mt-4 h-1.5 rounded-full" style="background:rgba(255,255,255,.08)"><div class="h-full rounded-full" style="width:{{ subject_progress.get(subject,0) }}%;background:linear-gradient(90deg,{{ info.meta.color_from }},{{ info.meta.color_to }});"></div></div>
      <div class="flex justify-between mt-2 text-xs text-muted"><span>{{ subject_progress.get(subject,0) }}% {{ 'पूर्ण' if language == 'hi' else 'complete' }}</span><span>→</span></div>
    </a>
    {% endfor %}
  </div>
  <div class="mt-6 p-4 rounded-2xl text-xs" style="background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.18);color:#b9b2ce;">
    {{ 'पाठ्यक्रम CBSE 2026–27 के प्रकाशित दस्तावेज़ों पर आधारित है। भाषा साहित्य के पाठ स्कूल द्वारा चुनी गई पुस्तक/कोर्स के अनुसार बदल सकते हैं।' if language == 'hi' else 'Curriculum structure is based on CBSE 2026–27 publications. Language-literature units depend on the textbook/course selected by your school.' }}
    <a href="{{ syllabus_url }}" target="_blank" rel="noopener" style="color:#c9b8ff;text-decoration:underline;">{{ 'आधिकारिक CBSE पाठ्यक्रम' if language == 'hi' else 'Official CBSE curriculum' }}</a>
  </div>
</div>
{{ nav_html | safe }}
</body></html>
"""

LEARNING_SUBJECT_TEMPLATE = r"""
<!DOCTYPE html><html lang="{{ 'hi' if language == 'hi' else 'en' }}"><head>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{{ display_subject }} — StudyTracker</title>
""" + BASE_STYLE + r"""
<style>
.subject-wrap{max-width:900px;margin:0 auto;padding:26px 16px 112px}
.subject-hero{background:radial-gradient(ellipse at 88% 0%,rgba(139,92,246,.3),transparent 48%),linear-gradient(135deg,#1b1631,#10111c);border:1px solid rgba(177,140,255,.2);border-radius:24px;padding:23px}
.chapter-link{display:flex;align-items:center;gap:14px;padding:16px;border:1px solid rgba(177,140,255,.13);border-radius:16px;background:linear-gradient(145deg,rgba(30,27,48,.9),rgba(16,17,29,.95));transition:transform .16s ease,border-color .16s ease}
.chapter-link:active{transform:scale(.99)}.chapter-link:hover{border-color:rgba(177,140,255,.42)}
.chapter-num{width:36px;height:36px;flex:0 0 36px;display:grid;place-items:center;border-radius:11px;background:rgba(139,92,246,.16);border:1px solid rgba(177,140,255,.24);color:#d9cbff;font-size:12px;font-weight:900}
</style></head><body><main class="subject-wrap">
<a href="/learn?grade={{ grade }}" class="text-xs font-bold text-muted">← {{ 'अध्ययन लाइब्रेरी' if language == 'hi' else 'Learning Library' }}</a>
<header class="subject-hero mt-4 mb-5"><div class="text-xs font-black tracking-widest" style="color:#C9EAFF">CBSE · CLASS {{ grade }}</div><h1 class="text-2xl font-black text-white mt-3">{{ display_subject }}</h1><p class="text-sm mt-2" style="color:#b7b0ca">{{ chapters|length }} {{ 'अध्याय' if language == 'hi' else 'chapters' }} · {{ 'नोट्स और माइंड मैप खोलने के लिए अध्याय चुनें' if language == 'hi' else 'Select a chapter to open its notes and mind map' }}</p></header>
<div class="space-y-3">{% for chapter in chapters %}<a class="chapter-link" href="{{ url_for('chapter_studio', grade=grade, subject=subject, chapter_index=loop.index0) }}"><span class="chapter-num">{{ '%02d'|format(loop.index) }}</span><span class="min-w-0 flex-1"><span class="block text-sm font-bold text-white">{{ chapter_names[loop.index0] }}</span><span class="block text-xs text-muted mt-1">{{ 'अध्यायवार नोट्स · माइंड मैप · अभ्यास' if language == 'hi' else 'Chapter notes · mind map · practice' }}</span></span><span style="color:#C9EAFF">→</span></a>{% endfor %}</div>
</main>{{ nav_html | safe }}</body></html>
"""

CHAPTER_STUDIO_TEMPLATE = r"""
<!DOCTYPE html><html lang="{{ 'hi' if language == 'hi' else 'en' }}"><head>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{{ pack.display_chapter if pack.display_chapter is defined else chapter }} — StudyTracker</title>
""" + BASE_STYLE + r"""
<style>
.study-wrap{max-width:900px;margin:0 auto;padding:24px 16px 112px}
.study-hero{background:radial-gradient(ellipse at 90% 0%,rgba(105,177,225,.25),transparent 48%),linear-gradient(135deg,#142A3A,#0A1420);border:1px solid rgba(169,223,255,.25);border-radius:24px;padding:22px}
.study-panel{background:rgba(16,28,41,.88);border:1px solid rgba(169,223,255,.18);border-radius:18px;padding:18px}
.study-tabs{display:flex;gap:8px;overflow:auto;padding:4px 0 12px}
.study-tabs button{white-space:nowrap;padding:10px 14px;border-radius:12px;background:rgba(255,255,255,.05);color:#A9C9DE;font-size:12px;font-weight:800}
.study-tabs button.active{background:linear-gradient(135deg,#A9DFFF,#477FA3);color:white}
.mind-node{background:linear-gradient(135deg,rgba(169,223,255,.17),rgba(80,150,195,.10));border:1px solid rgba(169,223,255,.25);border-radius:13px;padding:12px;text-align:center;color:#E7F5FF;font-size:12px;font-weight:700}
.answer{display:none;margin-top:10px;padding:12px;border-radius:12px;background:rgba(0,220,160,.08);color:#a8e8d6;font-size:13px}
.answer.show{display:block}
</style>
</head><body>
<div lang="{{ language }}" class="study-wrap">
  <a href="/learn?grade={{ grade }}" class="text-xs text-muted font-bold">← {{ 'लर्निंग लाइब्रेरी' if language == 'hi' else 'Learning Library' }}</a>
  <div class="study-hero mt-4 mb-5">
    <span class="studio-pill">CBSE · {{ 'कक्षा' if language == 'hi' else 'CLASS' }} {{ grade }}</span>
    <p class="text-xs uppercase tracking-widest mt-4" style="color:#C9EAFF;font-weight:800;">{{ display_subject if display_subject is defined else subject }}</p>
    <h1 class="text-2xl font-black text-white mt-2">{{ pack.display_chapter if pack.display_chapter is defined else chapter }}</h1>
    <p class="text-sm mt-3" style="color:#b7b0ca;">{{ pack.intro }}</p>
    <div class="flex gap-2 flex-wrap mt-4"><span class="studio-pill">{{ 'अध्याय' if language == 'hi' else 'CHAPTER' }} {{ chapter_index + 1 }}</span><span class="studio-pill">{{ 'विस्तृत अध्ययन नोट्स' if language == 'hi' else 'DETAILED STUDY NOTES' }}</span></div>
  </div>
  <div class="study-tabs">
    <button class="active" onclick="showTab('notes',this)">{{ 'नोट्स' if language == 'hi' else 'Notes' }}</button>
    <button onclick="showTab('mindmap',this)">{{ 'माइंड मैप' if language == 'hi' else 'Mind Map' }}</button>
    <button onclick="showTab('practice',this)">{{ 'प्रश्न अभ्यास' if language == 'hi' else 'Practice' }}</button>
    <button onclick="showTab('pyq',this)">{{ 'PYQ-शैली' if language == 'hi' else 'PYQ-style' }}</button>
  </div>
  <section id="tab-notes" class="study-panel">
    <h2 class="text-lg font-black text-white mb-3">{{ 'अध्याय का विस्तृत परिचय' if language == 'hi' else 'Chapter introduction' }}</h2>
    <p class="text-sm leading-7" style="color:#c6c0d5;">{{ pack.overview }}</p>
    <h3 class="text-sm font-black text-white mt-6 mb-3">{{ 'विस्तृत अध्याय नोट्स' if language == 'hi' else 'Detailed chapter notes' }}</h3>
    <ul class="space-y-3">{% for note in pack.notes %}<li class="flex gap-3 text-sm leading-6" style="color:#c6c0d5;"><span aria-hidden="true" style="width:7px;height:7px;min-width:7px;margin-top:8px;border-radius:2px;background:linear-gradient(135deg,#D9F2FF,#75BFEA);transform:rotate(45deg)"></span><span>{{ note }}</span></li>{% endfor %}</ul>
    <div class="mt-5 p-3 rounded-xl text-xs" style="background:rgba(255,190,90,.08);border:1px solid rgba(255,190,90,.18);color:#e7c994;">{{ 'ये विस्तृत सहायक अध्ययन नोट्स हैं। पूर्ण पाठ्यपुस्तक कवरेज, निर्धारित उदाहरणों और अभ्यासों के लिए अपनी NCERT पुस्तक के साथ इनका अध्ययन करें।' if language == 'hi' else 'These are expanded learning notes. Study them alongside your prescribed NCERT textbook for complete syllabus coverage, textbook examples and exercises.' }}</div>
  </section>
  <section id="tab-mindmap" class="study-panel" style="display:none">
    <h2 class="text-lg font-black text-white mb-2">{{ 'अध्याय माइंड मैप' if language == 'hi' else 'Chapter mind map' }}</h2>
    <div class="mind-node max-w-xs mx-auto mb-4">{{ chapter }}</div>
    <div class="grid grid-cols-2 md:grid-cols-3 gap-3">{% for node in pack.mindmap %}<div class="mind-node">{{ node }}</div>{% endfor %}</div>
    <p class="text-xs text-muted mt-4">{{ 'हर शाखा के नीचे अपनी पुस्तक से उदाहरण और सूत्र जोड़ें।' if language == 'hi' else 'Expand each branch with examples, diagrams and formulae from your textbook.' }}</p>
  </section>
  <section id="tab-practice" class="study-panel" style="display:none">
    <h2 class="text-lg font-black text-white mb-2">{{ 'अध्याय अभ्यास' if language == 'hi' else 'Chapter practice' }}</h2>
    <p class="text-xs text-muted mb-4">{{ 'उत्तर कॉपी में लिखें और फिर पुस्तक से जाँचें।' if language == 'hi' else 'Write your answers in a notebook, then verify them against your textbook.' }}</p>
    {% for q in pack.practice %}<div class="p-4 rounded-xl mb-3" style="background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.07)"><div class="text-xs mb-2" style="color:#A9DFFF">{{ 'प्रश्न' if language == 'hi' else 'QUESTION' }} {{ loop.index }}</div><p class="text-sm text-white leading-6">{{ q }}</p></div>{% endfor %}
  </section>
  <section id="tab-pyq" class="study-panel" style="display:none">
    <h2 class="text-lg font-black text-white mb-2">{{ 'परीक्षा-शैली अभ्यास' if language == 'hi' else 'Exam-style practice' }}</h2>
    <p class="text-sm leading-6 mb-4" style="color:#c6c0d5;">{{ pack.caution }}</p>
    <div class="space-y-3">{% for q in pack.practice %}<div class="p-4 rounded-xl" style="background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.07)"><p class="text-sm text-white">{{ q }}</p><p class="text-xs text-muted mt-2">{{ 'प्रकार: वर्णनात्मक / अनुप्रयोग आधारित' if language == 'hi' else 'Type: descriptive / application-based' }}</p></div>{% endfor %}</div>
    <a class="inline-flex mt-4 text-xs font-bold underline" style="color:#A9DFFF" href="{{ syllabus_url }}" target="_blank" rel="noopener">{{ 'CBSE आधिकारिक पाठ्यक्रम देखें' if language == 'hi' else 'Check official CBSE curriculum' }} →</a>
  </section>
</div>
<script>
function showTab(name,button){
  document.querySelectorAll('[id^="tab-"]').forEach(x=>x.style.display='none');
  document.getElementById('tab-'+name).style.display='block';
  document.querySelectorAll('.study-tabs button').forEach(x=>x.classList.remove('active'));
  button.classList.add('active');
}
</script>
{{ nav_html | safe }}
</body></html>
"""


@app.route('/learn')
def learning_studio():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    grade = request.args.get('grade') or get_student_grade(user.id)
    grade = "9" if grade in ("9", "IX", "ix") else "10"
    curriculum = get_grade_curriculum(grade)
    progress = get_user_progress(user.id)
    subject_progress = {}
    total_chapters = 0
    for subject, info in curriculum.items():
        total_chapters += len(info["chapters"])
        done = sum(1 for key, value in progress.items() if value and key.startswith(subject + "__"))
        subject_progress[subject] = round(done / (len(info["chapters"]) * CIRCLES_PER_CHAPTER) * 100, 1) if info["chapters"] else 0
    return render_page(
        LEARNING_STUDIO_TEMPLATE, user=user, grade=grade, curriculum=curriculum,
        subject_progress=subject_progress, total_chapters=total_chapters,
        language=session.get("language","en"), session_label=CBSE_SYLLABUS_SESSION,
        syllabus_url=CBSE_SYLLABUS_URL, nav_html=bottom_nav_html("hub")
    )


@app.route('/learn/<grade>/<path:subject>')
def learning_subject(grade, subject):
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    grade = "9" if grade in ("9", "IX", "ix") else "10"
    curriculum = get_grade_curriculum(grade)
    if subject not in curriculum:
        return redirect(url_for('learning_studio', grade=grade))
    language = session.get("language", "en")
    chapters = curriculum[subject]["chapters"]
    chapter_names = [HINDI_TITLES.get(name, name) if language == "hi" else name for name in chapters]
    display_subject = HINDI_TITLES.get(subject, subject.replace("English —", "अंग्रेज़ी —")) if language == "hi" else subject
    return render_page(
        LEARNING_SUBJECT_TEMPLATE, user=user, grade=grade, subject=subject,
        display_subject=display_subject, chapters=chapters, chapter_names=chapter_names,
        language=language, nav_html=bottom_nav_html("hub")
    )


@app.route('/study/<grade>/<path:subject>/<int:chapter_index>')
def chapter_studio(grade, subject, chapter_index):
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    grade = "9" if grade in ("9", "IX", "ix") else "10"
    curriculum = get_grade_curriculum(grade)
    if subject not in curriculum or not 0 <= chapter_index < len(curriculum[subject]["chapters"]):
        return redirect(url_for('learning_studio', grade=grade))
    chapter = curriculum[subject]["chapters"][chapter_index]
    pack = get_chapter_study_pack(grade, subject, chapter)
    return render_page(
        CHAPTER_STUDIO_TEMPLATE, user=user, grade=grade, subject=subject,
        chapter=chapter, chapter_index=chapter_index, pack=pack,
        display_subject=HINDI_TITLES.get(subject, subject) if session.get("language","en") == "hi" else subject,
        language=session.get("language","en"), syllabus_url=CBSE_SYLLABUS_URL,
        nav_html=bottom_nav_html("hub")
    )


@app.route('/set-language', methods=['POST'])
def set_language():
    data = request.get_json(silent=True) or request.form
    language = (data.get("language") or "en").lower()
    session["language"] = "hi" if language in ("hi", "hindi") else "en"
    return jsonify({"success": True, "language": session["language"]})


@app.route('/set-class', methods=['POST'])
def set_class():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    data = request.get_json(silent=True) or request.form
    grade = str(data.get("grade", "10"))
    if grade not in ("9", "10"):
        return jsonify({"success": False, "error": "Choose Class 9 or Class 10"}), 400
    user.student_class = "Class " + grade
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"success": False, "error": "Could not update class"}), 500
    return jsonify({"success": True, "grade": grade})



# 
# HTTP ROUTES
# 

@app.route('/healthz')
def healthz():
    return jsonify({"status": "ok"})


@app.route('/')
def index():
    user = get_current_user()
    if not user:
        return redirect(url_for('onboarding'))
    touch_user(user)
    update_streak(user)
    curriculum = get_full_curriculum(user.id)
    stats = compute_stats(user.id, curriculum)
    circ = 251.33
    ring_dash = round((stats['overall_pct'] / 100) * circ, 2)
    ring_gap = round(circ - ring_dash, 2)
    week_data = get_7day_data(user.id)
    week_total = sum(d['count'] for d in week_data)
    week_max = max((d['count'] for d in week_data), default=0)
    exam_plans = compute_exam_plans(user.id, curriculum, stats)
    nearest_exam = None
    for p in exam_plans:
        if p['status'] in ('upcoming', 'today'):
            nearest_exam = p; break
    return render_page(
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
    return render_page(
        ONBOARDING_TEMPLATE, error=None,
        online_count=get_live_online_count(),
        total_users=get_total_user_count()
    )


@app.route('/setup', methods=['POST'])
def setup():
    name = request.form.get('name', '').strip()
    student_class = request.form.get('student_class', '').strip()
    if not name or not student_class:
        return render_page(
            ONBOARDING_TEMPLATE, error="Please fill in all fields.",
            online_count=get_live_online_count(), total_users=get_total_user_count()
        )
    if len(name) > 100 or len(student_class) > 50:
        return render_page(
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
        return render_page(
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
    for p in compute_exam_plans(user.id, curriculum):
        if p['subject'] == subject_name:
            exam_plan = p; break
    return render_page(
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
        icon=request.form.get('icon', '')[:10],
        color_from=request.form.get('color_from', '#A9DFFF')[:20],
        color_to=request.form.get('color_to', '#75BFEA')[:20],
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
    current_curriculum = get_full_curriculum(user.id)
    if not cs and subject_name in current_curriculum:
        info = current_curriculum[subject_name]
        meta = info['meta']
        cs = CustomSubject(
            user_id=user.id, name=subject_name,
            icon=meta['icon'], color_from=meta['color_from'],
            color_to=meta['color_to'], tag=meta['tag']
        )
        db.session.add(cs)
        try:
            db.session.flush()
            for i, ch_name in enumerate(info['chapters']):
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
    return render_page(
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
            "user_avatar_color": m.user_avatar_color or '#A9DFFF',
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
    online_users_data = [{"name": u['name'], "student_class": u['student_class'], "avatar_color": u.get('avatar_color', '#A9DFFF')} for u in online_users_list]
    return render_page(
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
    # One aggregate query instead of one COUNT query per chapter.
    chapter_rows = db.session.query(
        ChapterProgress.subject, ChapterProgress.chapter_index,
        func.count(ChapterProgress.id)
    ).filter_by(user_id=user.id, completed=True).group_by(
        ChapterProgress.subject, ChapterProgress.chapter_index
    ).all()
    chapter_counts = {(subject, ci): count for subject, ci, count in chapter_rows}
    chapter_done_map = {
        subject: {ci: chapter_counts.get((subject, ci), 0)
                  for ci in range(len(info['chapters']))}
        for subject, info in curriculum.items()
    }
    week_data = get_7day_data(user.id)
    week_total = sum(d['count'] for d in week_data)
    week_avg = round(week_total / 7, 1)
    week_max = max((d['count'] for d in week_data), default=0)
    return render_page(
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
    custom_ids = [cs.id for cs in customs]
    chapter_count_rows = (db.session.query(CustomChapter.subject_id, func.count(CustomChapter.id))
                          .filter(CustomChapter.subject_id.in_(custom_ids))
                          .group_by(CustomChapter.subject_id).all()) if custom_ids else []
    chapter_count_map = dict(chapter_count_rows)
    custom_subjects = []
    for cs in customs:
        count = chapter_count_map.get(cs.id, 0)
        custom_subjects.append({
            "id": cs.id, "name": cs.name, "icon": cs.icon,
            "chapter_count": count
        })
    return render_page(
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
    color = request.form.get('color', '#A9DFFF')[:20]
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



# Global language selector and presentation polish. Language is stored per signed-in session.
@app.before_request
def keep_login_session_permanent():
    # Lightweight timing marker; logs only slow server-side requests on Render.
    import time
    request._st_started_at = time.perf_counter()
    if session.get("user_id"):
        session.permanent = True

@app.after_request
def log_slow_requests(response):
    import time
    started = getattr(request, "_st_started_at", None)
    if started is not None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= 700:
            app.logger.warning("SLOW_REQUEST method=%s path=%s status=%s elapsed_ms=%.0f",
                               request.method, request.path, response.status_code, elapsed_ms)
    return response

@app.after_request
def studytracker_response_polish(response):
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        lang = session.get("language", "en")
        toolbar = """
        <div id="st-global-tools" style="position:fixed;top:12px;right:12px;z-index:1000;display:flex;gap:7px;align-items:center;padding:6px 8px;border:1px solid rgba(177,140,255,.22);border-radius:14px;background:rgba(16,15,27,.88);backdrop-filter:blur(16px);box-shadow:0 8px 28px rgba(0,0,0,.25)">
          <a href="/learn" style="display:flex;align-items:center;gap:6px;font-size:11px;font-weight:800;color:#c9b8ff;text-decoration:none;padding:5px 7px"><span style="display:grid;place-items:center;width:18px;height:18px;border-radius:6px;background:linear-gradient(135deg,#A9DFFF,#397DA8);color:white;font-size:8px;letter-spacing:-.5px">ST</span>Learn</a>
          <select aria-label="Language" id="st-language" style="background:#27213e;color:#f4f0ff;border:1px solid rgba(177,140,255,.3);border-radius:9px;padding:6px 7px;font-size:11px;font-weight:800">
            <option value="en">English</option><option value="hi">हिन्दी</option>
          </select>
        </div>
        """
        toolbar = toolbar.replace('<option value="en">English</option>', '<option value="en"{}>English</option>'.format(' selected' if lang == 'en' else ''))
        toolbar = toolbar.replace('<option value="hi">हिन्दी</option>', '<option value="hi"{}>हिन्दी</option>'.format(' selected' if lang == 'hi' else ''))
        css = """
        <style id="st-premium-overrides">
        :root{color-scheme:dark;--st-accent:#A9DFFF}
        body{background:#0b0b13!important}
        .card-base,.chapter-row,.modal-content{background:linear-gradient(145deg,rgba(27,25,43,.94),rgba(16,17,28,.96))!important;border-color:rgba(177,160,255,.13)!important;box-shadow:0 12px 34px rgba(0,0,0,.18)}
        .gradient-btn{background:linear-gradient(115deg,#8b5cf6,#5945c7)!important;box-shadow:0 8px 24px rgba(112,77,220,.24)}
        .gradient-text{background:linear-gradient(100deg,#c4b5fd,#8b7bff,#e3b5ff)!important;-webkit-background-clip:text!important;background-clip:text!important}
        .glass-nav{background:rgba(12,12,21,.88)!important;border-top:1px solid rgba(177,160,255,.12)!important;backdrop-filter:blur(18px)!important}
        .subject-card,.nav-icon,.btn-secondary{transition:transform .16s ease,border-color .16s ease,background .16s ease}
        .subject-card:active,.btn-secondary:active{transform:scale(.985)}
        html{scroll-behavior:smooth;scrollbar-gutter:stable}
        *{-webkit-tap-highlight-color:transparent}
        button,a,select{touch-action:manipulation}
        @media(prefers-reduced-motion:no-preference){.fade-in{animation-duration:.28s!important}.chapter-bar,.bar-chart-bar{transition:width .18s ease,transform .18s ease!important}}
        @media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;animation-duration:.01ms!important;transition-duration:.01ms!important}}
        @media(max-width:520px){#st-global-tools{top:8px;right:8px;transform:scale(.94);transform-origin:top right}.scroll-content{padding-top:20px}}

        /* Mobile performance guard: preserve the glass look with cheaper compositing. */
        @media(max-width:700px){
          .glass-nav{backdrop-filter:blur(9px)!important;-webkit-backdrop-filter:blur(9px)!important}
          .card-base,.chapter-row,.modal-content{box-shadow:0 7px 18px rgba(0,0,0,.16)!important}
          .fade-in{animation:none!important}
        }
        @media(prefers-reduced-transparency:reduce){
          .glass-nav{backdrop-filter:none!important;-webkit-backdrop-filter:none!important;background:rgba(12,15,28,.96)!important}
        }
        </style>
        """
        translator = r"""
        <script>
        (function(){
          const dict = {
            "Welcome back ":"वापसी पर स्वागत है ","Overall Progress":"कुल प्रगति",
            "Past 7 Days":"पिछले 7 दिन","Daily learning activity":"दैनिक अध्ययन गतिविधि",
            "Subject Chambers":"विषय कक्ष","Add Subject":"विषय जोड़ें","Create custom chamber":"कस्टम विषय बनाएँ",
            "Join Live Chat":"लाइव चैट में शामिल हों","Settings":"सेटिंग्स","Manage your profile & data":"अपनी प्रोफ़ाइल और डेटा प्रबंधित करें",
            "Analytics":"विश्लेषण","Detailed performance overview":"प्रदर्शन का विस्तृत विवरण",
            "Exam Zone":"परीक्षा क्षेत्र","Plan your syllabus around exam dates":"परीक्षा तिथियों के अनुसार पाठ्यक्रम की योजना बनाएँ",
            "Schedule New Exam":"नई परीक्षा निर्धारित करें","No Exams Scheduled":"कोई परीक्षा निर्धारित नहीं है",
            "Add your first exam date and get a personalized daily study target.":"पहली परीक्षा तिथि जोड़ें और दैनिक अध्ययन लक्ष्य पाएँ।",
            "Your Study Plan":"आपकी अध्ययन योजना","Save Changes":"बदलाव सहेजें",
            "Update Profile":"प्रोफ़ाइल अपडेट करें","Avatar Color":"अवतार का रंग",
            "Danger Zone":"खतरे वाला क्षेत्र","This will permanently delete all your data.":"इससे आपका सारा डेटा स्थायी रूप से हट जाएगा।",
            "Reset All Data & Start Over":"सारा डेटा रीसेट करें और फिर शुरू करें",
            "Subject Sync":"विषय प्रगति","CUSTOM":"कस्टम","Done":"पूर्ण",
            "Read":"पढ़ें","Note":"नोट्स","Prac":"अभ्यास","Rev":"दोहराएँ",
            "Add Chapter":"अध्याय जोड़ें","Custom Chapter":"कस्टम अध्याय",
            "No chapters yet":"अभी कोई अध्याय नहीं","Add your first chapter to start tracking progress.":"प्रगति ट्रैक करने के लिए पहला अध्याय जोड़ें।",
            "Name":"नाम","Class":"कक्षा","Your Name":"आपका नाम","Your Class":"आपकी कक्षा",
            "Launch My Dashboard":"मेरा डैशबोर्ड खोलें","Save":"सहेजें",
            "Syllabus completion":"पाठ्यक्रम पूर्णता","Completed Units":"पूर्ण इकाइयाँ","Total Units":"कुल इकाइयाँ",
            "Study Streak":"अध्ययन निरंतरता","Chapters Done":"पूर्ण अध्याय","Online Now":"अभी ऑनलाइन",
            "Schedule Exam":"परीक्षा निर्धारित करें","Select Subject":"विषय चुनें","Exam Date":"परीक्षा तिथि",
            "Profile updated successfully!":"प्रोफ़ाइल सफलतापूर्वक अपडेट हुई!"
          };
          function translateNode(node){
            if(node.nodeType===Node.TEXT_NODE){
              const raw=node.nodeValue, key=raw.trim();
              if(dict[key]) node.nodeValue=raw.replace(key,dict[key]);
            } else if(node.nodeType===Node.ELEMENT_NODE && !['SCRIPT','STYLE','TEXTAREA','INPUT','SELECT','OPTION'].includes(node.tagName)){
              node.childNodes.forEach(translateNode);
              ['placeholder','title','aria-label'].forEach(attr=>{
                const v=node.getAttribute(attr); if(v && dict[v]) node.setAttribute(attr,dict[v]);
              });
            }
          }
          const lang = document.getElementById('st-language');
          if(lang){
            lang.addEventListener('change',async()=>{
              try{await fetch('/set-language',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({language:lang.value})});location.reload();}
              catch(e){alert('Could not change language. Please try again.');}
            });
          }
          const useHindi = document.documentElement.lang==='hi';
          if(useHindi && document.body) translateNode(document.body);
          // The script is injected immediately before </body>; translate once only.
        })();
        </script>
        """
        body = response.get_data(as_text=True)
        if lang == "hi":
            body = body.replace('<html lang="en">', '<html lang="hi">', 1)
        else:
            body = body.replace('<html lang="hi">', '<html lang="en">', 1)
        if "</head>" in body:
            body = body.replace("</head>", css + "</head>", 1)
        if "</body>" in body:
            body = body.replace("</body>", toolbar + translator + "</body>", 1)
        response.set_data(body)
    return response



# 
# SOCKETIO REAL-TIME EVENTS (with app_context)
# 

def broadcast_presence():
    users = get_live_online_users()
    users_public = [{
        "name": u['name'],
        "student_class": u['student_class'],
        "avatar_color": u.get('avatar_color', '#A9DFFF')
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
            'avatar_color': user.avatar_color or '#A9DFFF',
            'room': None,
            'connected_at': datetime.utcnow()
        }
        touch_user(user)
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
        "avatar_color": u.get('avatar_color', '#A9DFFF')
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
            user_avatar_color=user.avatar_color or '#A9DFFF',
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


# 
# DB INIT + AUTO MIGRATION (Works on SQLite + PostgreSQL)
# 

def init_db():
    """Safe DB initialization — never destroys data, works on both DBs."""
    with app.app_context():
        try:
            db.create_all()
            print(" Database tables ensured")
        except Exception as e:
            print(f"  create_all warning: {e}")

        from sqlalchemy import inspect, text
        try:
            inspector = inspect(db.engine)
            existing_tables = inspector.get_table_names()
        except Exception as e:
            print(f"  Could not inspect DB: {e}")
            return

        if 'user' not in existing_tables:
            print("ℹ  Fresh database — no migrations needed")
            return

        try:
            existing_cols = [c['name'] for c in inspector.get_columns('user')]
        except Exception as e:
            print(f"  Could not read columns: {e}")
            return

        needed = {
            'created_at':   'TIMESTAMP',
            'last_active':  'DATE',
            'last_seen':    'TIMESTAMP',
            'streak':       'INTEGER DEFAULT 0',
            'avatar_color': "VARCHAR(20) DEFAULT '#A9DFFF'"
        }

        migrations = []
        for col_name, col_type in needed.items():
            if col_name not in existing_cols:
                # Quote 'user' table name (reserved word in PostgreSQL)
                table_ref = '"user"' if IS_POSTGRES else 'user'
                migrations.append((col_name, f"ALTER TABLE {table_ref} ADD COLUMN {col_name} {col_type}"))

        if not migrations:
            print(" Schema up-to-date")
            return

        print(f" Running {len(migrations)} migration(s)...")
        for col_name, stmt in migrations:
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(stmt))
                print(f"    Added column: {col_name}")
            except Exception as e:
                print(f"     Skipped {col_name}: {e}")

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
                conn.execute(text(f"UPDATE {table_ref} SET avatar_color = '#A9DFFF' WHERE avatar_color IS NULL"))
            print(" Backfill complete")
        except Exception as e:
            print(f"  Backfill warning: {e}")


# Render free web services sleep after ~15 minutes without traffic. A request
# to our own health endpoint every 10 minutes keeps the instance warm.
def _start_render_keep_alive():
    if os.environ.get('KEEP_ALIVE', '1') == '0':
        return
    url = os.environ.get('RENDER_EXTERNAL_URL')
    if not url:
        host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
        if host:
            url = 'https://' + host
    if not url:
        return
    endpoint = url.rstrip('/') + '/healthz'

    def ping_loop():
        from urllib.request import urlopen
        while True:
            time.sleep(600)
            try:
                urlopen(endpoint, timeout=20).read()
            except Exception:
                pass

    threading.Thread(target=ping_loop, daemon=True, name='render-keepalive').start()


# Initialize DB immediately when this module is imported (needed for Gunicorn on Render)
init_db()
_start_render_keep_alive()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = not IS_POSTGRES
    print(f" StudyTracker running on http://0.0.0.0:{port}")
    print(" Real-time chat enabled (async_mode: threading)")
    socketio.run(
        app,
        host='0.0.0.0',
        port=port,
        debug=debug_mode,
        allow_unsafe_werkzeug=True,
        use_reloader=False
    )
