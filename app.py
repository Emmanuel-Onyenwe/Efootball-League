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
from threading import Thread

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'supersecretkey')

# --- PERMANENT DATABASE CONFIGURATION ---
uri = os.environ.get("DATABASE_URL", "sqlite:///league.db")
if uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = uri

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
    emblem = db.Column(db.String(10), default='🛡️') # NEW: The Emoji Emblem
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
    player_a_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    player_b_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    score_a = db.Column(db.Integer, nullable=True)
    score_b = db.Column(db.Integer, nullable=True)
    screenshot_path = db.Column(db.String(500), nullable=True) 
    deadline = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='pending') 
    player_a = db.relationship('User', foreign_keys=[player_a_id])
    player_b = db.relationship('User', foreign_keys=[player_b_id])

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            print(f"Background email failed: {e}")

def send_email(to, subject, template):
    msg = Message(subject, recipients=[to], html=template)
    Thread(target=send_async_email, args=(app, msg)).start()
    
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
            # --- NEW: Silently notify the Admin ---
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
            # --- NEW: Trigger the special welcome category ---
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
    
    # Grab all pending matches
    all_pending = Match.query.filter_by(status='pending').all()
    
    # Split them into Home (Leg 1) and Away (Leg 2)
    leg_1 = []
    leg_2 = []
    seen_pairs = set()
    
    for m in all_pending:
        pair = tuple(sorted([m.player_a_id, m.player_b_id]))
        if pair not in seen_pairs:
            leg_1.append(m)
            seen_pairs.add(pair)
        else:
            leg_2.append(m)
            
    # A quick helper function to sort any list of matches into Matchdays
    def interleave(match_list):
        interleaved = []
        temp = match_list.copy()
        while temp:
            current_players = set()
            removed = []
            for m in temp:
                if m.player_a_id not in current_players and m.player_b_id not in current_players:
                    interleaved.append(m)
                    current_players.add(m.player_a_id)
                    current_players.add(m.player_b_id)
                    removed.append(m)
            for m in removed:
                temp.remove(m)
            if not removed and temp: # Failsafe
                interleaved.append(temp.pop(0))
        return interleaved

    # Sort Leg 1, sort Leg 2, then stack them together!
    final_sorted_fixtures = interleave(leg_1) + interleave(leg_2)
    
    ticker_fixtures = final_sorted_fixtures 
    
    if current_user.is_authenticated:
        # Now it filters your matches from the perfectly sorted Matchday list!
        fixtures = [m for m in final_sorted_fixtures if m.player_a_id == current_user.id or m.player_b_id == current_user.id]
    else:
        fixtures = final_sorted_fixtures # Guests see the full 90, neatly sorted

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
    
    active_players = User.query.filter_by(status='active', in_league=True).all()
    pending_players = User.query.filter_by(in_league=False).all()
    pending_matches = Match.query.filter_by(status='submitted').all()
    
    # Grab all pending matches
    all_pending = Match.query.filter_by(status='pending').all()
    
    # Split them into Home (Leg 1) and Away (Leg 2)
    leg_1 = []
    leg_2 = []
    seen_pairs = set()
    
    for m in all_pending:
        pair = tuple(sorted([m.player_a_id, m.player_b_id]))
        if pair not in seen_pairs:
            leg_1.append(m)
            seen_pairs.add(pair)
        else:
            leg_2.append(m)
            
    # The Interleaving Engine
    def interleave(match_list):
        interleaved = []
        temp = match_list.copy()
        while temp:
            current_players = set()
            removed = []
            for m in temp:
                if m.player_a_id not in current_players and m.player_b_id not in current_players:
                    interleaved.append(m)
                    current_players.add(m.player_a_id)
                    current_players.add(m.player_b_id)
                    removed.append(m)
            for m in removed:
                temp.remove(m)
            if not removed and temp:
                interleaved.append(temp.pop(0))
        return interleaved

    # Stack Leg 1 and Leg 2 together for the Admin Board
    all_pending_fixtures = interleave(leg_1) + interleave(leg_2)
    
    return render_template('admin.html', active_players=active_players, pending_players=pending_players, pending_matches=pending_matches, all_pending_fixtures=all_pending_fixtures)

@app.route('/panic-hq/approve_player/<int:user_id>', methods=['POST'])
@login_required
def approve_player(user_id):
    if current_user.role == 'admin':
        user = User.query.get_or_404(user_id)
        user.in_league = True
        db.session.commit()
        
        # --- NEW: Notify user of approval ---
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
    users = User.query.filter_by(status='active', in_league=True).all()
    if len(users) < 2:
        flash("Need at least 2 approved players to generate fixtures.", "error")
        return redirect(url_for('admin'))
    
    # 'permutations' generates both Leg 1 (Home) and Leg 2 (Away)
    pairs = list(itertools.permutations(users, 2))
    base_deadline = datetime.now() + timedelta(days=7)
    matches_created = 0
    
    for player_a, player_b in pairs:
        existing_match = Match.query.filter_by(player_a_id=player_a.id, player_b_id=player_b.id).first()
        if not existing_match:
            db.session.add(Match(player_a_id=player_a.id, player_b_id=player_b.id, deadline=base_deadline))
            matches_created += 1
            
    db.session.commit()
    
    # --- NEW: Notify all active players about the new fixtures ---
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
    
    # Delete all matches 
    Match.query.delete()
    
    # Reset stats for all players
    users = User.query.all()
    for u in users:
        u.played = 0
        u.won = 0
        u.drawn = 0
        u.lost = 0
        u.gd = 0
        u.points = 0
        if u.role != 'admin':
            u.in_league = False  # Optionally send non-admins back to the waiting room
            
    db.session.commit()
    flash("League has been completely wiped and reset for a fresh season!", "success")
    return redirect(url_for('admin'))
    
# --- SECRET RESCUE ROUTE ---
@app.route('/panic-hq/rescue')
def rescue_founder():
    founder = User.query.get(1) # Gets the Head Admin (You)
    if founder:
        founder.status = 'active'
        founder.in_league = True
        founder.role = 'admin'
        db.session.commit()
        return "<h3>God Mode Activated. Head Admin Restored!</h3><a href='/panic-hq'>Click here to return to Control Room</a>"
    return "Head Admin not found."

# --- UPGRADED ELIMINATE FUNCTION ---
@app.route('/panic-hq/eliminate/<int:user_id>', methods=['POST'])
@login_required
def eliminate_player(user_id):
    if current_user.role == 'admin':
        # IMMUNITY FOR HEAD ADMIN
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

from sqlalchemy import text

@app.route('/edit_profile', methods=['POST'])
@login_required
def edit_profile():
    new_name = request.form.get('gamertag', '').strip()
    new_emblem = request.form.get('emblem')
    
    # 1. Check if the chosen emblem is taken by someone else
    taken_emblems = [u.emblem for u in User.query.all() if u.id != current_user.id]
    if new_emblem in taken_emblems:
        flash("That emblem is already taken by another player!", "error")
        return redirect(url_for('index'))
        
    # 2. Check Name Change Rules
    if new_name and new_name != current_user.name:
        if current_user.name_changed:
            flash("You have already used your one-time name change!", "error")
            return redirect(url_for('index'))
        
        # Check if the new Gamertag is already taken
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
        
        # Save info before deleting the user from the database
        target_email = user.email
        gamertag = user.name
        
        db.session.delete(user)
        db.session.commit()
        
        # --- NEW: Notify user of rejection ---
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
        # PostgreSQL uses FALSE instead of 0
        db.session.execute(text('ALTER TABLE "user" ADD COLUMN name_changed BOOLEAN DEFAULT FALSE'))
        db.session.commit()
        print("Successfully added name_changed column to database!")
    except Exception as e:
        # If it fails, it means the column already exists, so we just rollback and continue safely.
        db.session.rollback()
