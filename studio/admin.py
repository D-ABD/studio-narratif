from django.contrib import admin
from .models import BackgroundTrack, NarrationProject, NarrationSegment, Voice


class NarrationSegmentInline(admin.TabularInline):
    model = NarrationSegment
    extra = 0


@admin.register(Voice)
class VoiceAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "consent_confirmed", "created_at")
    list_filter = ("category", "consent_confirmed")
    search_fields = ("name",)


@admin.register(BackgroundTrack)
class BackgroundTrackAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "duration", "created_at")
    list_filter = ("created_at",)
    search_fields = ("name", "user__username")


@admin.register(NarrationProject)
class NarrationProjectAdmin(admin.ModelAdmin):
    list_display = ("title", "voice", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("title",)
    inlines = (NarrationSegmentInline,)
