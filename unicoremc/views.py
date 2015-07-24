import json
import requests

from apiclient import errors
from oauth2client.client import AccessTokenCredentialsError

from django.db.models import F
from django.shortcuts import render, get_object_or_404, redirect
from django.http import (
    HttpResponse, HttpResponseBadRequest, HttpResponseServerError,
    HttpResponseForbidden)
from django.contrib.auth.decorators import (
    login_required, permission_required, user_passes_test)
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import ListView
from django.views.generic.edit import UpdateView
from django.core.urlresolvers import reverse
from django.core.cache import cache
from django.contrib import messages

from unicoremc.models import Project, Localisation, AppType, ProjectRepo
from unicoremc.forms import ProjectForm
from unicoremc import constants, exceptions
from unicoremc import tasks, utils


def get_repos(refresh):
    if not refresh:
        return cache.get('repos')
    url = ('https://api.github.com/orgs/universalcore/'
           'repos?type=public&per_page=100&page=%s')
    pageNum = 1
    repos = []
    while True:
        response = requests.get(url % pageNum)
        data = response.json()
        if not data:
            break
        repos.extend(data)
        pageNum += 1
    repos = [{
        'name': r.get('name'),
        'git_url': r.get('git_url'),
        'clone_url': r.get('clone_url')
    } for r in repos]
    cache.set('repos', repos)
    return repos


def get_all_repos(request):
    refresh = request.GET.get('refresh', 'false') == 'true'
    repos = get_repos(refresh)
    return HttpResponse(json.dumps(repos), content_type='application/json')


@login_required
@permission_required('project.can_change')
@user_passes_test(
    lambda u: u.social_auth.filter(provider='github').exists(),
    login_url='/social/login/github/')
def new_project_view(request, *args, **kwargs):
    social = request.user.social_auth.get(provider='github')
    access_token = social.extra_data['access_token']
    context = {
        'countries': constants.COUNTRY_CHOICES,
        'languages': Localisation.objects.all(),
        'app_types': AppType.objects.all(),
        'project_repos': ProjectRepo.objects.filter(project__state='done'),
        'access_token': access_token,
    }
    return render(request, 'unicoremc/new_project.html', context)


class HomepageView(ListView):
    model = Project
    template_name = 'unicoremc/home.html'


class ProjectEditView(UpdateView):
    model = Project
    form_class = ProjectForm
    template_name = 'unicoremc/advanced.html'

    def get_success_url(self):
        return reverse("home")

    def get_object(self, queryset=None):
        return get_object_or_404(Project, pk=self.kwargs['project_id'])

    def form_valid(self, form):
        response = super(ProjectEditView, self).form_valid(form)
        project = self.get_object()
        Project.objects.filter(
            pk=project.pk).update(project_version=F('project_version') + 1)

        project = self.get_object()
        project.create_or_update_hub_app()
        project.create_pyramid_settings()
        project.create_nginx()

        try:
            project.update_marathon_app()
        except exceptions.MarathonApiException:
            messages.info(self.request, 'Unable to update project in marathon')
        return response


@csrf_exempt
@login_required
@permission_required('project.can_change')
@user_passes_test(
    lambda u: u.social_auth.filter(provider='github').exists(),
    login_url='/social/login/github/')
def start_new_project(request, *args, **kwargs):
    if request.method == 'POST':

        app_type = request.POST.get('app_type')
        app_type = AppType.objects.get(pk=int(app_type))
        base_repo = request.POST.get('base_repo')
        project_repos = request.POST.getlist('project_repos[]')
        repo_count = len(project_repos) + (1 if base_repo else 0)

        # validate base repos and app type
        if not repo_count:
            return HttpResponseBadRequest('No repo selected')
        if (repo_count > 1 and app_type.project_type == AppType.UNICORE_CMS):
            return HttpResponseBadRequest(
                '%s does not support multiple repos' % (AppType.UNICORE_CMS,))

        country = request.POST.get('country')
        access_token = request.POST.get('access_token')
        user_id = request.POST.get('user_id')
        team_id = request.POST.get('team_id')
        docker_cmd = request.POST.get('docker_cmd')

        user = User.objects.get(pk=user_id)

        project, created = Project.objects.get_or_create(
            application_type=app_type,
            country=country,
            defaults={
                'team_id': int(team_id),
                'owner': user,
                'docker_cmd':
                    docker_cmd or
                    utils.get_default_docker_cmd(app_type, country)
            })
        project.external_repos.add(*project_repos)
        if base_repo:
            ProjectRepo.objects.get_or_create(
                project=project,
                defaults={'base_url': base_repo})

        # For consistency with existing apps, all new apps will also have
        # country domain urls in addition to the generic urls
        project.frontend_custom_domain = project.get_country_domain()
        project.cms_custom_domain = 'cms.%s' % project.get_country_domain()
        project.save()

        if created:
            tasks.start_new_project.delay(project.id, access_token)

    return HttpResponse(json.dumps({'success': True}),
                        content_type='application/json')


@login_required
@permission_required('project.can_change')
@user_passes_test(
    lambda u: u.social_auth.filter(provider='google-oauth2').exists(),
    login_url='/social/login/google-oauth2/')
def manage_ga_view(request, *args, **kwargs):
    social = request.user.social_auth.get(provider='google-oauth2')
    access_token = social.extra_data['access_token']

    try:
        accounts = utils.get_ga_accounts(access_token)
    except AccessTokenCredentialsError:
        return redirect('/social/login/google-oauth2/')

    context = {
        'projects': Project.objects.filter(state='done'),
        'access_token': access_token,
        'accounts': [
            {'id': a.get('id'), 'name': a.get('name')} for a in accounts],
    }
    return render(request, 'unicoremc/manage_ga.html', context)


@csrf_exempt
@login_required
@permission_required('project.can_change')
@user_passes_test(
    lambda u: u.social_auth.filter(provider='google-oauth2').exists(),
    login_url='/social/login/google-oauth2/')
def manage_ga_new(request, *args, **kwargs):
    if request.method == 'POST':

        project_id = request.POST.get('project_id')
        account_id = request.POST.get('account_id')
        access_token = request.POST.get('access_token')
        project = get_object_or_404(Project, pk=project_id)

        if not project.ga_profile_id:
            try:
                name = u'%s %s' % (
                    project.app_type.upper(), project.get_country_display())
                new_profile_id = utils.create_ga_profile(
                    access_token, account_id, project.frontend_url(), name)

                project.ga_profile_id = new_profile_id
                project.ga_account_id = account_id
                project.save()
                project.create_pyramid_settings()

                return HttpResponse(
                    json.dumps({'ga_profile_id': new_profile_id}),
                    content_type='application/json')
            except errors.HttpError:
                return HttpResponseServerError("Unable to create new profile")

        return HttpResponseForbidden("Project already has a profile")

    return HttpResponseBadRequest("You can only call this using a POST")


@login_required
@permission_required('project.can_change')
def reset_hub_app_key(request, project_id):
    project = get_object_or_404(Project, pk=project_id)

    app = project.hub_app()
    if app is not None:
        app.reset_key()
        project.create_pyramid_settings()

    return redirect(reverse('advanced', args=(project_id, )))
