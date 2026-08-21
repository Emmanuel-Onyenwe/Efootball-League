import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecretkey'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///league.db'
app.config['UPLOAD_FOLDER'] = 'static/uploads'

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
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
    screenshot_path = db.Column(db.String(200), nullable=True)
    deadline = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='pending') 

    player_a = db.relationship('User', foreign_keys=[player_a_id])
    player_b = db.relationship('User', foreign_keys=[player_b_id])

# --- PHASE 2: CORE LOGIC ---
def update_standings():
    users = User.query.filter_by(status='active').all()
    for user in users:
        matches_as_a = Match.query.filter_by(player_a_id=user.id, status='approved').all()
        matches_as_b = Match.query.filter_by(player_b_id=user.id, status='approved').all()
        
        user.played = len(matches_as_a) + len(matches_as_b)
        user.won = user.drawn = user.lost = user.goals_for = user.goals_against = user.points = 0
        
        for m in matches_as_a:
            user.goals_for += m.score_a
            user.goals_against += m.score_b
            if m.score_a > m.score_b:
                user.won += 1; user.points += 3
            elif m.score_a == m.score_b:
                user.drawn += 1; user.points += 1
            else:
                user.lost += 1
                
        for m in matches_as_b:
            user.goals_for += m.score_b
            user.goals_against += m.score_a
            if m.score_b > m.score_a:
                user.won += 1; user.points += 3
            elif m.score_b == m.score_a:
                user.drawn += 1; user.points += 1
            else:
                user.lost += 1
    db.session.commit()

# --- PHASE 3: ROUTES ---
@app.route('/')
def index():
    update_standings()
    # Sort by Points, then Goal Difference
    users = User.query.filter_by(status='active').all()
    standings = sorted(users, key=lambda u: (u.points, (u.goals_for - u.goals_against)), reverse=True)
    fixtures = Match.query.filter_by(status='pending').all()
    return render_template('index.html', standings=standings, fixtures=fixtures)

@app.route('/submit', methods=['GET', 'POST'])
def submit():
    if request.method == 'POST':
        match_id = request.form.get('match_id')
        score_a = request.form.get('score_a')
        score_b = request.form.get('score_b')
        screenshot = request.files.get('screenshot')

        match = Match.query.get(match_id)
        if match and screenshot:
            filename = secure_filename(screenshot.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            screenshot.save(filepath)
            
            match.score_a = int(score_a)
            match.score_b = int(score_b)
            match.screenshot_path = filename
            match.status = 'submitted'
            db.session.commit()
            flash("Result submitted and pending admin approval!", "success")
            return redirect(url_for('index'))
            
    fixtures = Match.query.filter_by(status='pending').all()
    return render_template('submit.html', fixtures=fixtures)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)