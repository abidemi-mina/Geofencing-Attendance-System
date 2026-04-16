from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html

from .models import (
    AttendanceAttemptLog,
    AttendanceRecord,
    AttendanceSession,
    Course,
    CourseRegistration,
    DeviceBinding,
    RequestThrottleLog,
    RotatingSessionToken,
    StudentProfile,
    User,
)


# ─────────────────────────────────────────────────────────────
# Inlines
# ─────────────────────────────────────────────────────────────

class StudentProfileInline(admin.StackedInline):
    model = StudentProfile
    can_delete = False
    verbose_name = "Student Profile"
    fields = ["level"]


class CourseRegistrationInline(admin.TabularInline):
    model = CourseRegistration
    extra = 0
    readonly_fields = ["course", "registered_at"]
    can_delete = True


# ─────────────────────────────────────────────────────────────
# User admin
# ─────────────────────────────────────────────────────────────

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    model = User
    list_display  = ("matric_number", "full_name", "role_badge", "is_active", "is_staff", "date_joined")
    list_filter   = ("role", "is_active", "is_staff")
    ordering      = ("full_name",)
    search_fields = ("matric_number", "full_name", "email")
    inlines       = [StudentProfileInline]

    fieldsets = (
        (None, {"fields": ("matric_number", "password")}),
        ("Personal Information", {"fields": ("full_name", "email")}),
        ("Role & Access", {"fields": ("role", "is_active", "is_staff", "is_superuser")}),
        ("Permissions", {"classes": ("collapse",), "fields": ("groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "matric_number", "full_name", "role",
                "password1", "password2",
                "is_staff", "is_active",
            ),
        }),
    )

    @admin.display(description="Role")
    def role_badge(self, obj):
        color = "#1a3a6c" if obj.role == "admin" else "#15803d"
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;">{}</span>',
            color,
            obj.get_role_display(),
        )


# ─────────────────────────────────────────────────────────────
# Course admin
# ─────────────────────────────────────────────────────────────

@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display  = ("code", "title", "level", "lecturer", "total_students", "total_sessions", "created_at")
    list_filter   = ("level",)
    search_fields = ("code", "title", "lecturer__full_name")
    ordering      = ("code",)
    inlines       = [CourseRegistrationInline]

    @admin.display(description="Students")
    def total_students(self, obj):
        return obj.registrations.count()

    @admin.display(description="Sessions")
    def total_sessions(self, obj):
        return obj.sessions.count()


# ─────────────────────────────────────────────────────────────
# Attendance session admin
# ─────────────────────────────────────────────────────────────

@admin.register(AttendanceSession)
class AttendanceSessionAdmin(admin.ModelAdmin):
    list_display  = ("title", "course", "admin", "status_badge", "shape_type", "start_time", "end_time", "marked_count")
    list_filter   = ("status", "shape_type", "course__level")
    search_fields = ("title", "course__code", "admin__full_name")
    ordering      = ("-created_at",)
    readonly_fields = ("created_at",)

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {"active": "#15803d", "draft": "#1d4ed8", "closed": "#6b7280"}
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;">{}</span>',
            colors.get(obj.status, "#6b7280"),
            obj.get_status_display(),
        )

    @admin.display(description="Marked")
    def marked_count(self, obj):
        return obj.records.count()


# ─────────────────────────────────────────────────────────────
# Attendance record admin
# ─────────────────────────────────────────────────────────────

@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display  = ("student", "session", "status", "marked_at", "ip_address", "accuracy_meters", "is_mock_location")
    list_filter   = ("status", "is_mock_location", "session__course")
    search_fields = ("student__matric_number", "student__full_name", "session__title")
    ordering      = ("-marked_at",)
    readonly_fields = ("marked_at", "device_fingerprint", "ip_address", "validation_notes")


# ─────────────────────────────────────────────────────────────
# Rotating token admin
# ─────────────────────────────────────────────────────────────

@admin.register(RotatingSessionToken)
class RotatingSessionTokenAdmin(admin.ModelAdmin):
    list_display  = ("session", "is_active", "expires_at", "created_at")
    list_filter   = ("is_active",)
    search_fields = ("session__title",)
    ordering      = ("-created_at",)
    readonly_fields = ("token", "created_at")


# ─────────────────────────────────────────────────────────────
# Device binding admin
# ─────────────────────────────────────────────────────────────

@admin.register(DeviceBinding)
class DeviceBindingAdmin(admin.ModelAdmin):
    list_display  = ("student", "first_seen_at", "last_seen_at")
    search_fields = ("student__matric_number", "student__full_name")
    ordering      = ("-last_seen_at",)
    readonly_fields = ("fingerprint", "first_seen_at", "last_seen_at")


# ─────────────────────────────────────────────────────────────
# Attempt log admin
# ─────────────────────────────────────────────────────────────

@admin.register(AttendanceAttemptLog)
class AttendanceAttemptLogAdmin(admin.ModelAdmin):
    list_display  = ("student", "session", "reason", "attempted_at", "ip_address")
    list_filter   = ("reason",)
    search_fields = ("student__matric_number", "reason", "ip_address")
    ordering      = ("-attempted_at",)
    readonly_fields = ("attempted_at", "device_fingerprint", "meta")


# ─────────────────────────────────────────────────────────────
# Throttle log admin
# ─────────────────────────────────────────────────────────────

@admin.register(RequestThrottleLog)
class RequestThrottleLogAdmin(admin.ModelAdmin):
    list_display = ("ip_address", "path", "created_at")
    list_filter  = ("path",)
    ordering     = ("-created_at",)


# ─────────────────────────────────────────────────────────────
# Student profile admin (standalone access)
# ─────────────────────────────────────────────────────────────

@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display  = ("user", "level")
    list_filter   = ("level",)
    search_fields = ("user__matric_number", "user__full_name")
    ordering      = ("level", "user__full_name")


# ─────────────────────────────────────────────────────────────
# Course registration admin
# ─────────────────────────────────────────────────────────────

@admin.register(CourseRegistration)
class CourseRegistrationAdmin(admin.ModelAdmin):
    list_display  = ("student", "course", "registered_at")
    list_filter   = ("course__code",)
    search_fields = ("student__matric_number", "student__full_name", "course__code")
    ordering      = ("course__code", "student__full_name")
    readonly_fields = ("registered_at",)