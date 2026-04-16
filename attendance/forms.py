import json

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import AttendanceSession, Course


# ─────────────────────────────────────────────────────────────
# Course form
# ─────────────────────────────────────────────────────────────

class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ["code", "title", "level"]
        widgets = {
            "code": forms.TextInput(attrs={
                "placeholder": "e.g. CSC 301",
                "maxlength": "20",
                "style": "text-transform:uppercase;",
            }),
            "title": forms.TextInput(attrs={
                "placeholder": "e.g. Introduction to Computer Science",
                "maxlength": "150",
            }),
            "level": forms.TextInput(attrs={
                "placeholder": "e.g. 300",
                "maxlength": "10",
            }),
        }
        labels = {
            "code":  "Course Code",
            "title": "Course Title",
            "level": "Student Level",
        }
        help_texts = {
            "code":  "Unique across the system. Automatically uppercased.",
            "level": "Must match the level in your student CSV uploads.",
        }

    def clean_code(self):
        code = self.cleaned_data.get("code", "").strip().upper()
        if not code:
            raise ValidationError("Course code is required.")
        return code

    def clean_title(self):
        title = self.cleaned_data.get("title", "").strip()
        if not title:
            raise ValidationError("Course title is required.")
        return title

    def clean_level(self):
        level = self.cleaned_data.get("level", "").strip()
        if not level:
            raise ValidationError("Student level is required.")
        return level


# ─────────────────────────────────────────────────────────────
# Attendance session form
# ─────────────────────────────────────────────────────────────

class AttendanceSessionForm(forms.ModelForm):
    class Meta:
        model = AttendanceSession
        fields = [
            "title",
            "course",
            "shape_type",
            "center_lat",
            "center_lng",
            "radius_meters",
            "start_time",
            "end_time",
            "late_after",
            "status",
        ]
        widgets = {
            "title": forms.TextInput(attrs={
                "placeholder": "e.g. Week 5 Lecture — CSC 301",
                "maxlength": "150",
            }),
            "start_time": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "end_time": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "late_after": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "center_lat": forms.NumberInput(attrs={
                "placeholder": "Latitude",
                "step": "any",
            }),
            "center_lng": forms.NumberInput(attrs={
                "placeholder": "Longitude",
                "step": "any",
            }),
            "radius_meters": forms.NumberInput(attrs={
                "placeholder": "e.g. 100",
                "min": "10",
                "step": "1",
            }),
        }
        labels = {
            "title":         "Session Title",
            "course":        "Course",
            "shape_type":    "Geofence Shape",
            "center_lat":    "Center Latitude",
            "center_lng":    "Center Longitude",
            "radius_meters": "Radius (metres)",
            "start_time":    "Start Time",
            "end_time":      "End Time",
            "late_after":    "Mark as Late After",
            "status":        "Initial Status",
        }

    def __init__(self, *args, **kwargs):
        lecturer = kwargs.pop("lecturer", None)
        super().__init__(*args, **kwargs)

        # Restrict course choices to only this lecturer's courses
        if lecturer is not None:
            self.fields["course"].queryset = Course.objects.filter(
                lecturer=lecturer
            ).order_by("code")
        else:
            self.fields["course"].queryset = Course.objects.none()

        # Make geofence fields not required at the form level —
        # we validate them contextually in clean()
        for f in ("center_lat", "center_lng", "radius_meters", "late_after"):
            self.fields[f].required = False

        # datetime fields: tell Django to parse the datetime-local format
        for f in ("start_time", "end_time", "late_after"):
            self.fields[f].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]

    def clean_title(self):
        title = self.cleaned_data.get("title", "").strip()
        if not title:
            raise ValidationError("Session title is required.")
        return title

    def clean(self):
        cleaned = super().clean()
        start_time    = cleaned.get("start_time")
        end_time      = cleaned.get("end_time")
        late_after    = cleaned.get("late_after")
        shape_type    = cleaned.get("shape_type")
        center_lat    = cleaned.get("center_lat")
        center_lng    = cleaned.get("center_lng")
        radius_meters = cleaned.get("radius_meters")

        # ── Time window ──────────────────────────────────────
        if start_time and end_time:
            if start_time >= end_time:
                self.add_error("end_time", "End time must be after start time.")

            if late_after:
                if not (start_time <= late_after <= end_time):
                    self.add_error(
                        "late_after",
                        "Late-after time must fall within the session window.",
                    )

        # ── Geofence ─────────────────────────────────────────
        if shape_type == "circle":
            if center_lat is None:
                self.add_error("center_lat", "Latitude is required for a circle geofence.")
            if center_lng is None:
                self.add_error("center_lng", "Longitude is required for a circle geofence.")
            if radius_meters is None:
                self.add_error("radius_meters", "Radius is required for a circle geofence.")
            elif radius_meters <= 0:
                self.add_error("radius_meters", "Radius must be greater than zero.")
            if center_lat is not None and not (-90 <= center_lat <= 90):
                self.add_error("center_lat", "Latitude must be between -90 and 90.")
            if center_lng is not None and not (-180 <= center_lng <= 180):
                self.add_error("center_lng", "Longitude must be between -180 and 180.")

        elif shape_type == "polygon":
            raw_polygon = self.data.get("polygon_points", "").strip()
            if not raw_polygon:
                raise ValidationError(
                    "Please draw a polygon boundary on the map (minimum 3 points)."
                )
            try:
                points = json.loads(raw_polygon)
            except (json.JSONDecodeError, ValueError):
                raise ValidationError("Polygon data is corrupted. Please redraw it.")

            if not isinstance(points, list) or len(points) < 3:
                raise ValidationError(
                    "The polygon must have at least 3 vertices. Please redraw it."
                )

            for pt in points:
                if not isinstance(pt, dict) or "lat" not in pt or "lng" not in pt:
                    raise ValidationError(
                        "Polygon data is malformed. Please clear and redraw the boundary."
                    )

        return cleaned