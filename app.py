from collections import defaultdict
from datetime import datetime, timedelta

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///league.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Standard win/draw/loss point values used by update_standings().
POINTS_WIN = 3
POINTS_DRAW = 1
POINTS_LOSS = 0


class User(db.Model):
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    points = db.Column(db.Integer, nullable=False, default=0)
    strikes = db.Column(db.Integer, nullable=False, default=0)
    # 'active' | 'eliminated' -- drives generate_fixtures() and eliminate_player()
    status = db.Column(db.String(20), nullable=False, default='active')

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


# ---------------------------------------------------------------------------
# Phase 2: Tournament logic
# ---------------------------------------------------------------------------

def generate_fixtures(start_date=None, days_between_rounds=7):
    """
    Round-robin scheduling (circle method) for all active Users.

    Pairs every active user against every other active user exactly once,
    spread across rounds so no user plays twice in the same round. Creates
    one Match record per pairing.

    Args:
        start_date: datetime for round 1's deadline. Defaults to now.
        days_between_rounds: gap in days between each round's deadline.

    Returns:
        List of the newly created Match objects (already committed).
    """
    if start_date is None:
        start_date = datetime.utcnow()

    players = [u.id for u in User.query.filter_by(status='active').all()]

    if len(players) < 2:
        return []

    # Circle method needs an even count; a bye (None) sits out each round.
    if len(players) % 2 != 0:
        players.append(None)

    n = len(players)
    num_rounds = n - 1
    half = n // 2

    created_matches = []
    rotation = players[:]  # working copy we rotate each round

    for round_index in range(num_rounds):
        deadline = start_date + timedelta(days=days_between_rounds * round_index)

        for i in range(half):
            player_a_id = rotation[i]
            player_b_id = rotation[n - 1 - i]

            if player_a_id is None or player_b_id is None:
                continue  # this pairing is the bye -- no match created

            match = Match(
                player_a_id=player_a_id,
                player_b_id=player_b_id,
                deadline=deadline,
                status='pending',
            )
            db.session.add(match)
            created_matches.append(match)

        # Rotate everyone except the fixed first element.
        rotation.insert(1, rotation.pop())

    db.session.commit()
    return created_matches


def update_standings():
    """
    Recalculates total points and Points Per Game (PPG) for every user
    from their completed Match records (win=3, draw=1, loss=0), persists
    `points` on each User, and returns a sorted standings table.

    Returns:
        List of dicts sorted by points then PPG (descending), e.g.:
        [{'user_id': 1, 'name': 'Emma', 'points': 9, 'games_played': 3,
          'ppg': 3.0}, ...]
    """
    stats = defaultdict(lambda: {'points': 0, 'games': 0})

    completed_matches = Match.query.filter_by(status='completed').all()

    for match in completed_matches:
        if match.score_a is None or match.score_b is None:
            continue  # incomplete data -- skip rather than guess

        stats[match.player_a_id]['games'] += 1
        stats[match.player_b_id]['games'] += 1

        if match.score_a > match.score_b:
            stats[match.player_a_id]['points'] += POINTS_WIN
            stats[match.player_b_id]['points'] += POINTS_LOSS
        elif match.score_a < match.score_b:
            stats[match.player_a_id]['points'] += POINTS_LOSS
            stats[match.player_b_id]['points'] += POINTS_WIN
        else:
            stats[match.player_a_id]['points'] += POINTS_DRAW
            stats[match.player_b_id]['points'] += POINTS_DRAW

    standings = []
    all_users = User.query.all()

    for user in all_users:
        user_stats = stats.get(user.id, {'points': 0, 'games': 0})
        user.points = user_stats['points']  # persist onto the User row

        games_played = user_stats['games']
        ppg = round(user_stats['points'] / games_played, 2) if games_played else 0.0

        standings.append({
            'user_id': user.id,
            'name': user.name,
            'points': user_stats['points'],
            'games_played': games_played,
            'ppg': ppg,
        })

    db.session.commit()

    standings.sort(key=lambda row: (-row['points'], -row['ppg']))
    return standings


def eliminate_player(user_id):
    """
    If the given user has reached 2+ strikes, marks them 'eliminated' and
    deletes their future, unplayed Match records (status != 'completed'),
    so the league stays fair for everyone else's remaining fixtures.
    Past, completed matches are left untouched.

    Args:
        user_id: the User.id to evaluate.

    Returns:
        True if the user was eliminated, False if they weren't (either
        not found or under the strike threshold).
    """
    user = User.query.get(user_id)
    if user is None:
        return False

    if user.strikes < 2:
        return False

    user.status = 'eliminated'

    unplayed_matches = Match.query.filter(
        ((Match.player_a_id == user_id) | (Match.player_b_id == user_id)),
        Match.status != 'completed',
    ).all()

    for match in unplayed_matches:
        db.session.delete(match)

    db.session.commit()
    return True


# Ensure the SQLite file and tables exist as soon as the app runs.
with app.app_context():
    db.create_all()


if __name__ == '__main__':
    app.run(debug=True)