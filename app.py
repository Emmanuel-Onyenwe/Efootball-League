import random
import os
import itertools
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import cloudinary
import cloudinary.uploader
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
from sqlalchemy import text

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'supersecretkey')

# --- PERMANENT DATABASE CONFIGURATION ---
uri = os.environ.get("DATABASE_URL", "sqlite:///league.db")
if uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = uri
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 300}

# --- EMAIL CONFIGURATION ---
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 465
app.config['MAIL_USE_TLS'] = False
app.config['MAIL_USE_SSL'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME')

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
mail = Mail(app)

s = URLSafeTimedSerializer(app.config['SECRET_KEY'])

# --- CLOUDINARY CONFIGURATION ---
cloudinary.config( 
  cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME'), 
  api_key = os.environ.get('CLOUDINARY_API_KEY'), 
  api_secret = os.environ.get('CLOUDINARY_API_SECRET') 
)

# --- DATABASE MODELS ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='player') 
    in_league = db.Column(db.Boolean, default=False) 
    is_verified = db.Column(db.Boolean, default=False)
    emblem = db.Column(db.String(10), default='🛡️')
    name_changed = db.Column(db.Boolean, default=False)
    
    # Stats
    points = db.Column(db.Integer, default=0)
    strikes = db.Column(db.Integer, default=0)
    played = db.Column(db.Integer, default=0)
    won = db.Column(db.Integer, default=0)
    drawn = db.Column(db.Integer, default=0)
    lost = db.Column(db.Integer, default=0)
    goals_for = db.Column(db.Integer, default=0)
    goals_against = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='active')

class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player_a_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    player_b_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    score_a = db.Column(db.Integer, default=0)
    score_b = db.Column(db.Integer, default=0)
    screenshot_path = db.Column(db.String(500), nullable=True) 
    deadline = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='pending') 
    matchday = db.Column(db.Integer, default=0)
    reminder_sent = db.Column(db.Boolean, default=False)
    
    player_a = db.relationship('User', foreign_keys=[player_a_id])
    player_b = db.relationship('User', foreign_keys=[player_b_id])

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- FULLY SYNCHRONOUS EMAIL SENDER (WSGI SAFE) ---
def send_email(to, subject, template):
    """
    Synchronous email sender. 
    Blocks the Gunicorn worker briefly to guarantee delivery.
    """
    msg = Message(subject, recipients=[to], html=template)
    try:
        mail.send(msg)
        print(f"SUCCESS: Email sent to {to}")
    except Exception as e:
        print(f"FAILED: Email could not be sent to {to}. Error: {e}")

# --- ROUND-ROBIN SCHEDULING (Circle / Berger method) ---
def generate_round_robin_schedule(player_ids):
    players = list(player_ids)
    n = len(players)
    if n < 2:
        return []
    if n % 2 != 0:
        raise ValueError("Round-robin scheduling requires an even number of players.")

    fixed = players[0]
    rotating = players[1:]
    leg1_rounds = []

    for r in range(n - 1):
        round_matches = []
        home, away = (fixed, rotating[-1]) if r % 2 == 0 else (rotating[-1], fixed)
        round_matches.append((home, away))

        for i in range((n // 2) - 1):
            p1, p2 = rotating[i], rotating[-(i + 2)]
            home, away = (p1, p2) if i % 2 == 0 else (p2, p1)
            round_matches.append((home, away))

        leg1_rounds.append(round_matches)
        rotating.insert(0, rotating.pop())

    leg2_rounds = [[(away, home) for (home, away) in rnd] for rnd in leg1_rounds]
    return leg1_rounds + leg2_rounds

def get_pending_fixtures_sorted():
    return Match.query.filter_by(status='pending').order_by(Match.matchday, Match.id).all()

# --- AUTHENTICATION ROUTES ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        gamertag = request.form.get('gamertag')
        email = request.form.get('email')
        password = request.form.get('password')
        emblem = request.form.get('emblem', '🛡️')
        
        if User.query.filter_by(email=email).first() or User.query.filter_by(name=gamertag).first():
            flash("Email or Gamertag already taken.", "error")
            return redirect(url_for('index', show='register'))
            
        hashed_pw = generate_password_hash(password)
        is_first_user = User.query.count() == 0
        role = 'admin' if is_first_user else 'player'
        
        new_user = User(name=gamertag, email=email, password_hash=hashed_pw, role=role, in_league=is_first_user, is_verified=True, emblem=emblem)
        db.session.add(new_user)
        db.session.commit()

        if is_first_user:
            flash("Admin account created!", "success")
        else:
            try:
                admin_user = User.query.filter_by(role='admin').first()
                if admin_user:
                    admin_msg = f"<h3>New Player Alert!</h3><p><b>{gamertag}</b> ({email}) just registered for the league and is waiting in your control room.</p>"
                    send_email(admin_user.email, f"New Registration: {gamertag}", admin_msg)
            except Exception as e:
                print(f"Admin notification failed: {e}")
                
            flash("Registration successful! An Admin must approve your account before you can log in.", "success")
            
        return redirect(url_for('index', show='login'))
    return redirect(url_for('index', show='register'))

@app.route('/verify_email/<token>')
def verify_email(token):
    try:
        email = s.loads(token, salt='email-confirm', max_age=3600)
        user = User.query.filter_by(email=email).first_or_404()
        if user.is_verified:
            flash("Account already verified. Please log in.", "success")
        else:
            user.is_verified = True
            db.session.commit()
            flash("Email verified successfully! An admin will review your entry soon.", "success")
    except SignatureExpired:
        flash("The verification link has expired. Please register again.", "error")
    except BadTimeSignature:
        flash("Invalid verification link.", "error")
    return redirect(url_for('index', show='login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first() 
        
        if user and check_password_hash(user.password_hash, password):
            if not user.in_league:
                flash("Your account is still waiting for Admin approval.", "error")
                return redirect(url_for('index', show='login'))
                
            login_user(user)
            flash(f"Welcome back, {user.name}! 🎮", "welcome")
            return redirect(url_for('index'))
        else:
            flash("Invalid email or password.", "error")
            return redirect(url_for('index', show='login'))
    return redirect(url_for('index', show='login'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()
        if user:
            token = s.dumps(email, salt='password-reset')
            link = url_for('reset_password', token=token, _external=True)
            html_msg = f"<h3>Password Reset Request</h3><p>Click the link below to reset your Panic Keh password:</p><a href='{link}'>Reset Password</a><p>If you didn't request this, ignore this email.</p>"
            send_email(email, "Reset Your Password", html_msg)
        flash("If an account exists with that email, a reset link has been sent.", "success")
        return redirect(url_for('login'))
    return render_template('forgot.html') 

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = s.loads(token, salt='password-reset', max_age=3600)
    except:
        flash("The reset link is invalid or has expired.", "error")
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first_or_404()
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        flash("Your password has been updated! You can now log in.", "success")
        return redirect(url_for('login'))
    return render_template('reset.html') 

# --- CORE LOGIC (Standings) ---
def update_standings():
    users = User.query.filter_by(status='active', in_league=True).all()
    for user in users:
        matches_as_a = Match.query.filter_by(player_a_id=user.id, status='approved').all()
        matches_as_b = Match.query.filter_by(player_b_id=user.id, status='approved').all()
        user.played = len(matches_as_a) + len(matches_as_b)
        user.won = user.drawn = user.lost = user.goals_for = user.goals_against = user.points = 0
        for m in matches_as_a:
            user.goals_for += m.score_a; user.goals_against += m.score_b
            if m.score_a > m.score_b: user.won += 1; user.points += 3
            elif m.score_a == m.score_b: user.drawn += 1; user.points += 1
            else: user.lost += 1
        for m in matches_as_b:
            user.goals_for += m.score_b; user.goals_against += m.score_a
            if m.score_b > m.score_a: user.won += 1; user.points += 3
            elif m.score_b == m.score_a: user.drawn += 1; user.points += 1
            else: user.lost += 1
    db.session.commit()

# --- PUBLIC ROUTES ---
@app.route('/')
def index():
    update_standings()
    users = User.query.filter_by(status='active', in_league=True).all()
    
    for u in users:
        u.gd = u.goals_for - u.goals_against
        u.ppg = round(u.points / u.played, 2) if u.played > 0 else 0.0

    standings = sorted(users, key=lambda u: (u.ppg, u.gd), reverse=True)
    final_sorted_fixtures = get_pending_fixtures_sorted()
    ticker_fixtures = final_sorted_fixtures

    if current_user.is_authenticated:
        fixtures = [m for m in final_sorted_fixtures if m.player_a_id == current_user.id or m.player_b_id == current_user.id]
    else:
        fixtures = final_sorted_fixtures 

    completed_matches = Match.query.filter_by(status='approved').order_by(Match.id.desc()).all()
    return render_template('index.html', standings=standings, fixtures=fixtures, completed_matches=completed_matches, ticker_fixtures=ticker_fixtures)

@app.route('/submit', methods=['GET', 'POST'])
@login_required
def submit():
    if request.method == 'POST':
        match_id = request.form.get('match_id')
        score_a = request.form.get('score_a')
        score_b = request.form.get('score_b')
        screenshot = request.files.get('screenshot')

        match = Match.query.get(match_id)
        if match and screenshot:
            upload_result = cloudinary.uploader.upload(screenshot)
            match.score_a = int(score_a)
            match.score_b = int(score_b)
            match.screenshot_path = upload_result['secure_url']
            match.status = 'submitted'
            db.session.commit()
            flash("Result submitted and pending admin approval!", "success")
            return redirect(url_for('index'))
            
    fixtures = Match.query.filter(
        (Match.status == 'pending') & 
        ((Match.player_a_id == current_user.id) | (Match.player_b_id == current_user.id))
    ).all()
    return render_template('submit.html', fixtures=fixtures)

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    if filename.startswith('http'):
        return redirect(filename)
    return "File not found", 404

# --- ADMIN DASHBOARD ---
@app.route('/panic-hq')
@login_required
def admin():
    if current_user.role != 'admin':
        flash("Access Denied: Admins only.", "error")
        return redirect(url_for('index'))
    
    active_players = User.query.filter_by(status='active', in_league=True).order_by(User.name).all()
    pending_players = User.query.filter_by(in_league=False).order_by(User.id).all()
    pending_matches = Match.query.filter_by(status='submitted').all()
    all_pending_fixtures = get_pending_fixtures_sorted()

    return render_template('admin.html', active_players=active_players, pending_players=pending_players, pending_matches=pending_matches, all_pending_fixtures=all_pending_fixtures)

@app.route('/panic-hq/approve_player/<int:user_id>', methods=['POST'])
@login_required
def approve_player(user_id):
    if current_user.role == 'admin':
        user = User.query.get_or_404(user_id)
        user.in_league = True
        db.session.commit()
        
        try:
            msg = f"<h3>You are in! 🎮</h3><p>Your registration for the Panic Keh League has been officially approved. You can now log in to the dashboard to check your stats and fixtures.</p>"
            send_email(user.email, "Welcome to the League!", msg)
        except Exception as e:
            print(f"Approval email failed: {e}")
            
        flash(f"{user.name} added to the league roster!", "success")
    return redirect(url_for('admin'))

@app.route('/panic-hq/promote/<int:user_id>', methods=['POST'])
@login_required
def promote_player(user_id):
    if current_user.role == 'admin':
        user = User.query.get_or_404(user_id)
        user.role = 'admin'
        db.session.commit()
        flash(f"{user.name} is now a Co-Admin!", "success")
    return redirect(url_for('admin'))

@app.route('/panic-hq/generate_fixtures', methods=['POST'])
@login_required
def generate_fixtures():
    if current_user.role != 'admin': return redirect(url_for('index'))
    users = User.query.filter_by(status='active', in_league=True).order_by(User.id).all()
    if len(users) < 2:
        flash("Need at least 2 approved players to generate fixtures.", "error")
        return redirect(url_for('admin'))
    if len(users) % 2 != 0:
        flash("The Circle Method schedule needs an EVEN number of active players. Approve/remove a player first.", "error")
        return redirect(url_for('admin'))

    user_ids = [u.id for u in users]
    schedule = generate_round_robin_schedule(user_ids)
    
    # Calculate the end of TODAY (11:59:59 PM)
    now = datetime.now()
    end_of_today = now.replace(hour=23, minute=59, second=59, microsecond=0)
    
    matches_created = 0

    for matchday_index, round_matches in enumerate(schedule, start=1):
        # Matchday 1 expires tonight, Matchday 2 expires tomorrow night, etc.
        matchday_deadline = end_of_today + timedelta(days=(matchday_index - 1))
        
        for home_id, away_id in round_matches:
            existing_match = Match.query.filter_by(player_a_id=home_id, player_b_id=away_id).first()
            if not existing_match:
                db.session.add(Match(
                    player_a_id=home_id,
                    player_b_id=away_id,
                    deadline=matchday_deadline,
                    matchday=matchday_index,
                    status='pending'
                ))
                matches_created += 1

    db.session.commit()
    
    if matches_created > 0:
        for u in users:
            try:
                msg = f"<h3>Matchday Alert!</h3><p>New fixtures have just been generated for the Panic Keh League. Log in to check your opponent and coordinate your match!</p>"
                send_email(u.email, "New League Fixtures Generated!", msg)
            except Exception as e:
                print(f"Failed to email {u.email}: {e}")
                
    flash(f"Generated {matches_created} new fixtures successfully! Players have been notified via email.", "success")
    return redirect(url_for('admin'))
    
@app.route('/panic-hq/approve/<int:match_id>', methods=['POST'])
@login_required
def approve_match(match_id):
    if current_user.role == 'admin':
        match = Match.query.get_or_404(match_id)
        match.status = 'approved'
        db.session.commit()
        flash("Match result approved and standings updated!", "success")
    return redirect(url_for('admin'))

@app.route('/panic-hq/reject/<int:match_id>', methods=['POST'])
@login_required
def reject_match(match_id):
    if current_user.role == 'admin':
        match = Match.query.get_or_404(match_id)
        match.status = 'pending'
        match.score_a = None
        match.score_b = None
        match.screenshot_path = None
        db.session.commit()
        flash("Match rejected and reset. Players must re-submit.", "error")
    return redirect(url_for('admin'))

@app.route('/panic-hq/reset', methods=['POST'])
@login_required
def reset_league():
    if current_user.role != 'admin':
        abort(403)
    
    Match.query.delete()
    
    users = User.query.all()
    for u in users:
        u.played = 0
        u.won = 0
        u.drawn = 0
        u.lost = 0
        u.gd = 0
        u.points = 0
        u.goals_for = 0
        u.goals_against = 0
        u.strikes = 0
        if u.role != 'admin':
            u.in_league = False  
            
    db.session.commit()
    flash("League has been completely wiped and reset for a fresh season!", "success")
    return redirect(url_for('admin'))

@app.route('/panic-hq/add_strike/<int:user_id>', methods=['POST'])
@login_required
def add_strike(user_id):
    if current_user.role == 'admin':
        user = User.query.get_or_404(user_id)
        user.strikes += 1
        db.session.commit()
        flash(f"Strike added to {user.name}. Total strikes: {user.strikes}", "error")
    return redirect(url_for('admin'))

@app.route('/panic-hq/remove_strike/<int:user_id>', methods=['POST'])
@login_required
def remove_strike(user_id):
    if current_user.role == 'admin':
        user = User.query.get_or_404(user_id)
        user.strikes = max(0, user.strikes - 1)
        db.session.commit()
        flash(f"Strike removed from {user.name}. Total strikes: {user.strikes}", "success")
    return redirect(url_for('admin'))

@app.route('/panic-hq/reset_strikes', methods=['POST'])
@login_required
def reset_strikes():
    if current_user.role != 'admin':
        flash("Access Denied: Admins only.", "error")
        return redirect(url_for('index'))
    users = User.query.all()
    for u in users:
        u.strikes = 0
    db.session.commit()
    flash("All player strikes have been cleared.", "success")
    return redirect(url_for('admin'))

@app.route('/panic-hq/admin_override', methods=['POST'])
@login_required
def admin_override():
    if current_user.id != 1:
        flash("Access Denied: Head Admin Only", "error")
        return redirect(url_for('admin'))

    match_id = request.form.get('match_id')
    action = request.form.get('action')
    
    match = Match.query.get_or_404(match_id)
    
    if action == 'void':
        db.session.delete(match)
        flash("Match successfully voided.", "success")
    elif action == 'walkover_home':
        match.score_a = 3
        match.score_b = 0
        match.status = 'approved'
        flash(f"Walkover awarded: {match.player_a.name} wins 3-0.", "success")
    elif action == 'walkover_away':
        match.score_a = 0
        match.score_b = 3
        match.status = 'approved'
        flash(f"Walkover awarded: {match.player_b.name} wins 3-0.", "success")
        
    db.session.commit()
    update_standings()
    return redirect(url_for('admin'))
    
@app.route('/panic-hq/rescue')
def rescue_founder():
    founder = User.query.get(1)
    if founder:
        founder.status = 'active'
        founder.in_league = True
        founder.role = 'admin'
        db.session.commit()
        return "<h3>God Mode Activated. Head Admin Restored!</h3><a href='/panic-hq'>Click here to return to Control Room</a>"
    return "Head Admin not found."

@app.route('/panic-hq/eliminate/<int:user_id>', methods=['POST'])
@login_required
def eliminate_player(user_id):
    if current_user.role == 'admin':
        if user_id == 1:
            flash("ACCESS DENIED: You cannot eliminate the Head Admin.", "error")
            return redirect(url_for('admin'))
            
        user = User.query.get_or_404(user_id)
        user.status = 'eliminated'
        unplayed_matches = Match.query.filter(
            (Match.status == 'pending') & 
            ((Match.player_a_id == user.id) | (Match.player_b_id == user.id))
        ).all()
        for m in unplayed_matches:
            db.session.delete(m)
        db.session.commit()
        flash(f"{user.name} eliminated! {len(unplayed_matches)} future matches were safely removed.", "success")
    return redirect(url_for('admin'))

@app.route('/edit_profile', methods=['POST'])
@login_required
def edit_profile():
    new_name = request.form.get('gamertag', '').strip()
    new_emblem = request.form.get('emblem')
    
    taken_emblems = [u.emblem for u in User.query.all() if u.id != current_user.id]
    if new_emblem in taken_emblems:
        flash("That emblem is already taken by another player!", "error")
        return redirect(url_for('index'))
        
    if new_name and new_name != current_user.name:
        if current_user.name_changed:
            flash("You have already used your one-time name change!", "error")
            return redirect(url_for('index'))
        
        if User.query.filter_by(name=new_name).first():
            flash("That Gamertag is already taken.", "error")
            return redirect(url_for('index'))
            
        current_user.name = new_name
        current_user.name_changed = True
        
    if new_emblem:
        current_user.emblem = new_emblem
        
    db.session.commit()
    flash("Profile updated successfully!", "success")
    return redirect(url_for('index'))
    
@app.route('/panic-hq/reject_player/<int:user_id>', methods=['POST'])
@login_required
def reject_player(user_id):
    if current_user.role == 'admin':
        user = User.query.get_or_404(user_id)
        
        target_email = user.email
        gamertag = user.name
        
        db.session.delete(user)
        db.session.commit()
        
        try:
            msg = f"<h3>Registration Update</h3><p>Unfortunately, your registration for the Panic Keh League under the Gamertag <b>{gamertag}</b> was declined.</p>"
            send_email(target_email, "Panic Keh Registration Update", msg)
        except Exception as e:
            print(f"Rejection email failed: {e}")
            
        flash(f"Registration for {gamertag} was rejected and deleted.", "success")
    return redirect(url_for('admin'))

# --- AUTO-PATCH DATABASE ON STARTUP ---
with app.app_context():
    try:
        db.session.execute(text('ALTER TABLE "user" ADD COLUMN name_changed BOOLEAN DEFAULT FALSE'))
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        # Added quotes around "match"
        db.session.execute(text('ALTER TABLE "match" ADD COLUMN matchday INTEGER DEFAULT 0'))
        db.session.commit()
    except Exception:
        db.session.rollback()
        
    try:
        # Added quotes around "match"
        db.session.execute(text('ALTER TABLE "match" ADD COLUMN reminder_sent BOOLEAN DEFAULT FALSE'))
        db.session.commit()
    except Exception:
        db.session.rollback()

@app.route('/panic-hq/sync-matchdays', methods=['POST'])
@login_required
def sync_matchdays():
    if current_user.role != 'admin':
        flash("Access Denied: Admins only.", "error")
        return redirect(url_for('index'))

    try:
        db.session.execute(text('ALTER TABLE match ADD COLUMN matchday INTEGER DEFAULT 0'))
        db.session.commit()
    except Exception:
        db.session.rollback()

    users = User.query.filter_by(status='active', in_league=True).order_by(User.id).all()
    user_ids = [u.id for u in users]

    if len(user_ids) % 2 != 0:
        flash("Can't sync: the Circle Method needs an EVEN number of active players.", "error")
        return redirect(url_for('admin'))

    schedule = generate_round_robin_schedule(user_ids)

    pair_matchdays = {} 
    for matchday_index, round_matches in enumerate(schedule, start=1):
        for home_id, away_id in round_matches:
            pair = tuple(sorted([home_id, away_id]))
            pair_matchdays.setdefault(pair, []).append(matchday_index)

    all_matches = Match.query.order_by(Match.id).all()
    matches_by_pair = {}
    for m in all_matches:
        pair = tuple(sorted([m.player_a_id, m.player_b_id]))
        matches_by_pair.setdefault(pair, []).append(m)

    updated = 0
    created = 0
    
    now = datetime.now()
    end_of_today = now.replace(hour=23, minute=59, second=59, microsecond=0)

    for pair, matchdays in pair_matchdays.items():
        existing = matches_by_pair.get(pair, [])
        for leg_index, matchday in enumerate(matchdays):
            
            # The exact midnight deadline for this specific matchday
            matchday_deadline = end_of_today + timedelta(days=(matchday - 1))
            
            if leg_index < len(existing):
                m = existing[leg_index]
                # Force update both the matchday number AND the new midnight deadline
                if m.matchday != matchday or m.deadline != matchday_deadline:
                    m.matchday = matchday
                    m.deadline = matchday_deadline 
                    updated += 1
            else:
                home_id, away_id = next(
                    (h, a) for (h, a) in schedule[matchday - 1] if tuple(sorted([h, a])) == pair
                )
                db.session.add(Match(
                    player_a_id=home_id, player_b_id=away_id,
                    deadline=matchday_deadline, status='pending', matchday=matchday
                ))
                created += 1

    db.session.commit()
    flash(f"Matchdays synced: {updated} existing matches corrected, {created} missing fixtures created. No matches were deleted.", "success")
    return redirect(url_for('admin'))

# --- CRON ROUTE: DEADLINE REMINDERS (BULK SEND FIX) ---
@app.route('/api/cron/deadline-reminders')
def cron_deadline_reminders():
    """
    Pings the database to find matches expiring in < 24 hours.
    Opens a SINGLE secure connection to Gmail to bulk-send all reminders.
    """
    now = datetime.now()
    tomorrow = now + timedelta(hours=24)
    
    matches = Match.query.filter(
        Match.status == 'pending',
        Match.deadline != None,
        Match.deadline <= tomorrow,
        Match.deadline > now,
        Match.reminder_sent == False
    ).all()

    if not matches:
        return "Cron Executed: 0 fixtures processed for reminders.", 200

    reminders_sent = 0
    
    try:
        # Open ONE single connection to Gmail to prevent rate-limiting timeouts
        with mail.connect() as conn:
            for match in matches:
                subject = f"⚠️ URGENT: Match Deadline Approaching!"
                html_msg = f"""
                <h3>Panic Keh League Alert</h3>
                <p>Your Matchday {match.matchday} fixture (<b>{match.player_a.name} vs {match.player_b.name}</b>) is expiring in less than 24 hours!</p>
                <p>Please coordinate with your opponent, play the match, and submit the result on the dashboard immediately to avoid a penalty strike.</p>
                """
                
                # Create and send Player A's email
                msg_a = Message(subject, recipients=[match.player_a.email], html=html_msg)
                conn.send(msg_a)
                
                # Create and send Player B's email
                msg_b = Message(subject, recipients=[match.player_b.email], html=html_msg)
                conn.send(msg_b)
                
                match.reminder_sent = True
                reminders_sent += 1

        if reminders_sent > 0:
            db.session.commit()
            
        return f"Cron Executed: {reminders_sent} fixtures processed for reminders.", 200
        
    except Exception as e:
        print(f"CRON BULK EMAIL FAILED: {e}")
        return f"Cron Failed: {e}", 500
