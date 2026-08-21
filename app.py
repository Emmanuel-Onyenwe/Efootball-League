from datetime import datetime

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///league.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    points = db.Column(db.Integer, nullable=False, default=0)
    strikes = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f'<User {self.id} {self.name}>'


class Match(db.Model):
    __tablename__ = 'match'

    id = db.Column(db.Integer, primary_key=True)
    player_a_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    player_b_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    score_a = db.Column(db.Integer, nullable=True)
    score_b = db.Column(db.Integer, nullable=True)
    deadline = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')

    player_a = db.relationship('User', foreign_keys=[player_a_id])
    player_b = db.relationship('User', foreign_keys=[player_b_id])

    def __repr__(self):
        return f'<Match {self.id} {self.player_a_id} vs {self.player_b_id}>'


@app.route('/')
def index():
    return 'League App Running'


# Ensure the SQLite file and tables exist as soon as the app starts.
with app.app_context():
    db.create_all()


if __name__ == '__main__':
    app.run(debug=True)