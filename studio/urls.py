from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("voices/create/", views.create_voice, name="create_voice"),
    path("voices/<int:voice_id>/edit/", views.edit_voice, name="edit_voice"),
    path("voices/<int:voice_id>/delete/", views.delete_voice, name="delete_voice"),
    path("projects/create/", views.create_project, name="create_project"),
    path("projects/<int:project_id>/", views.project_detail, name="project_detail"),
    path("projects/<int:project_id>/scenes/import/", views.import_scenes, name="import_scenes"),
    path("projects/<int:project_id>/assemble-videos/", views.assemble_videos, name="assemble_videos"),
    path("projects/<int:project_id>/video-options/", views.save_video_options, name="save_video_options"),
    path("projects/<int:project_id>/delete/", views.delete_project, name="delete_project"),
    path("projects/<int:project_id>/voice/", views.set_project_voice, name="set_project_voice"),
    path("projects/<int:project_id>/segments/add/", views.add_segment, name="add_segment"),
    path("projects/<int:project_id>/segments/reorder/", views.reorder_segments, name="reorder_segments"),
    path("projects/<int:project_id>/segments/<int:segment_id>/save/", views.save_segment, name="save_segment"),
    path("projects/<int:project_id>/segments/<int:segment_id>/delete/", views.delete_segment, name="delete_segment"),
    path("projects/<int:project_id>/prepare/", views.prepare, name="prepare"),
    path("projects/<int:project_id>/status/", views.project_status, name="project_status"),
    path("projects/<int:project_id>/segments/<int:segment_id>/subtitles.srt", views.segment_subtitles, name="segment_subtitles"),
]
