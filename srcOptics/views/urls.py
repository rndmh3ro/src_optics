from django.urls import path, re_path

from . import views, graph, util

urlpatterns = [
    path('', views.index),
    # path('repo/<str:repo_name>', graph.basic),
    path('repos/', views.attributes_by_repo, name='repos'),
    # path('repo_details/', views.repo_details, name='repo_details'),
    path('repos/<slug>/', views.repo_details, name='repo_details'),
    path('author/<slug>/', views.author_details, name='author_details'),
    path('q/<q>/', util.search, name='api'),
    path('add_repo', views.add_repo, name='add_repo')
]
