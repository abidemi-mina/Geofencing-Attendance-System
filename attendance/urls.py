from django.shortcuts import redirect
from django.urls import path

from .views import (
    MatricLoginView,
    course_export,
    course_records,
    course_roster_export,
    course_students,
    create_course,
    create_session,
    dashboard,
    logout_view,
    mark_attendance_api,
    search_students,
    session_detail,
    session_export,
    session_qr,
    session_token_api,
    student_mark_home,
    student_mark_page,
    toggle_session_status,
    upload_students,
)


def home_redirect(request):
    if request.user.is_authenticated:
        if request.user.role == "admin":
            return redirect("dashboard")
        return redirect("student-mark-home")
    return redirect("login")


urlpatterns = [
    # ── Root ────────────────────────────────────────────────
    path("", home_redirect, name="home"),

    # ── Auth ────────────────────────────────────────────────
    path("login/",  MatricLoginView.as_view(), name="login"),
    path("logout/", logout_view,               name="logout"),

    # ── Admin: dashboard ────────────────────────────────────
    path("dashboard/", dashboard, name="dashboard"),

    # ── Admin: courses ───────────────────────────────────────
    path("courses/create/",                          create_course,        name="create-course"),
    path("courses/<int:course_id>/students/",        course_students,      name="course-students"),
    path("courses/<int:course_id>/records/",         course_records,       name="course-records"),
    path("courses/<int:course_id>/export/",          course_export,        name="course-export"),
    path("courses/<int:course_id>/roster-export/",   course_roster_export, name="course-roster-export"),

    # ── Admin: students ──────────────────────────────────────
    path("students/upload/", upload_students, name="upload-students"),

    # ── Admin: sessions ──────────────────────────────────────
    path("sessions/create/",                      create_session,       name="create-session"),
    path("sessions/<int:session_id>/",            session_detail,       name="session-detail"),
    path("sessions/<int:session_id>/toggle/",     toggle_session_status, name="toggle-session"),
    path("sessions/<int:session_id>/token/",      session_token_api,    name="session-token-api"),
    path("sessions/<int:session_id>/qr/",         session_qr,           name="session-qr"),
    path("sessions/<int:session_id>/export/",     session_export,       name="session-export"),

    # ── Student ──────────────────────────────────────────────
    path("student/mark/",                 student_mark_home, name="student-mark-home"),
    path("student/mark/<int:session_id>/", student_mark_page, name="student-mark-page"),

    # ── APIs ─────────────────────────────────────────────────
    path("search-students/",                      search_students,     name="search-students"),
    path("api/sessions/<int:session_id>/mark/",   mark_attendance_api, name="mark-attendance-api"),
]