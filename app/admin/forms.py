from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional, URL

from app.models import NOTIFICATION_LEVELS, PERSPECTIVES, SOURCE_TYPES


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=100)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=200)])


class SourceForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=300)])
    type = SelectField("Type", choices=[(t, t) for t in SOURCE_TYPES], validators=[DataRequired()])
    url = StringField("URL", validators=[DataRequired(), Length(max=1000)])
    perspective = SelectField(
        "Perspective", choices=[(p, p) for p in PERSPECTIVES], validators=[DataRequired()]
    )
    reliability_tier = IntegerField(
        "Reliability tier (1 high – 3 low)", validators=[DataRequired(), NumberRange(min=1, max=3)]
    )
    poll_interval_s = IntegerField(
        "Poll interval (s)", validators=[DataRequired(), NumberRange(min=60, max=86400)]
    )
    enabled = BooleanField("Enabled")
    meta_json = TextAreaField("Meta (JSON)", validators=[Optional(), Length(max=4000)])


class NotificationForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired(), Length(max=300)])
    body = TextAreaField("Body", validators=[Optional(), Length(max=4000)])
    level = SelectField("Level", choices=[(l, l) for l in NOTIFICATION_LEVELS])
    active = BooleanField("Active", default=True)
    starts_at = DateTimeLocalField("Starts at (UTC)", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    ends_at = DateTimeLocalField("Ends at (UTC)", format="%Y-%m-%dT%H:%M", validators=[Optional()])


class BlackoutForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=200)])
    reason = TextAreaField("Reason", validators=[Optional(), Length(max=1000)])
    geojson = TextAreaField("Polygon GeoJSON", validators=[DataRequired()])
    active = BooleanField("Active", default=True)


class EventLocationForm(FlaskForm):
    lat = StringField("Latitude", validators=[DataRequired()])
    lon = StringField("Longitude", validators=[DataRequired()])
