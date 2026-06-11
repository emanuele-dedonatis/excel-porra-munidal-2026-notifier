from sqlalchemy import Column, Integer, String, DateTime, JSON, UniqueConstraint
from sqlalchemy.sql import func

from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    telegram_chat_id = Column(String, unique=True, nullable=False, index=True)
    # {str(match_number): {home_team, away_team, kickoff_utc, prediction,
    #                       predicted_home_goals, predicted_away_goals, round_label}}
    predictions = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class NotifiedMatch(Base):
    __tablename__ = "notified_matches"

    id = Column(Integer, primary_key=True)
    telegram_chat_id = Column(String, nullable=False, index=True)
    api_match_id = Column(Integer, nullable=False)
    # Match result
    home_team = Column(String)
    away_team = Column(String)
    home_score = Column(Integer)
    away_score = Column(Integer)
    duration = Column(String)           # REGULAR | EXTRA_TIME | PENALTY_SHOOTOUT
    # User's prediction
    prediction = Column(String)
    predicted_home_goals = Column(Integer)
    predicted_away_goals = Column(Integer)
    # 0 = wrong, 1 = correct result, 2 = exact score
    correct = Column(Integer)
    points = Column(Integer)   # actual points earned (based on group's scoring config)
    notified_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("telegram_chat_id", "api_match_id"),)
