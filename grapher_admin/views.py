import copy
import datetime
from dateutil import parser
import json
import re
import csv
import glob
import os
import subprocess
import threading
import shlex
import time
from ansi2html import Ansi2HTMLConverter
from unidecode import unidecode
from io import StringIO
import urllib
from urllib.parse import urlparse
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import login as loginview
from django.db.models import Q
from django.db import connection
from django.http import HttpRequest, HttpResponseRedirect, HttpResponse, HttpResponseNotFound, JsonResponse, QueryDict, StreamingHttpResponse
from django.template.response import TemplateResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from .forms import InviteUserForm, InvitedUserRegisterForm
from .models import Chart, Variable, User, UserInvitation, Logo, ChartSlugRedirect, ChartDimension, Dataset, Setting, DatasetCategory, DatasetSubcategory, Entity, Source, VariableType, DataValue, License
from typing import Dict, Union, Optional
from django.db import transaction
from django.core.cache import cache
import requests

def get_query_string(request):
    """
    :param request: Request object
    :return: The URL query string
    """
    return urlparse(request.get_full_path()).query

def get_query_as_dict(request):
    """
    :param request: Request object
    :return: The dictionary containing URL query parameters
    """
    return dict(urllib.parse.parse_qs(urllib.parse.urlsplit(request.get_full_path()).query))


def JsonErrorResponse(message: str, status: int = 400):
    return JsonResponse({
        "error": {
            "code": status,
            "message": message
        }
    }, status=status)

def dictfetchall(cursor):
    """
    :param cursor: Database cursor
    :return: Returns all rows from the cursor as a list of dicts
    """
    columns = [col[0] for col in cursor.description]
    return [
        dict(zip(columns, row))
        for row in cursor.fetchall()
    ]

def test_all(request):
    test_type = request.GET.get('type', '')
    test_tab = request.GET.get('tab', '')
    test_overlay = request.GET.get('overlay', '')
    test_page = request.GET.get('page', '')
    test_compare = request.GET.get('compare', '')

    if not test_tab and test_type == 'map':
        test_tab = 'map'
    elif not test_tab and test_type:
        test_tab = 'chart'

    if not test_page:
        test_page = 1
    else:
        try:
            test_page = int(test_page)
        except ValueError:
            test_page = 1

    if test_compare == '1':
        test_compare = 1
    else:
        test_compare = 0

    charts_per_page = 5

    query = Chart.objects.filter(config__isPublished=True).order_by('-created_at')

    if test_type and test_type != 'map':
        if test_type == "stackedarea":
            query = query.filter(config__type="StackedArea")
        if test_type == "scatter":
            query = query.filter(config__type="ScatterPlot")
        else:
            query = query.filter(config__type=test_type)

    urls = []
    count = 0

    for each in query:
        configfile = each.config
        if test_type == 'map' and not configfile.get('hasMapTab'):
            continue
        elif test_type and not configfile.get('hasChartTab'):
            continue

        count += 1
        local_url = request.build_absolute_uri('/grapher/') + each.config['slug']
        live_url = "https://ourworldindata.org/grapher/" + each.config['slug']
        local_url_png = local_url + '.png'
        live_url_png = live_url + '.png'

        if test_tab:
            local_url = local_url + '?tab=' + test_tab
            live_url = live_url + '?tab=' + test_tab
            local_url_png = local_url_png + '?tab=' + test_tab
            live_url_png = live_url_png + '?tab=' + test_tab

        if test_overlay:
            local_url = local_url + '?overlay=' + test_overlay
            live_url = live_url + '?overlay=' + test_overlay
            local_url_png = local_url_png + '?overlay=' + test_overlay
            live_url_png = live_url_png + '?overlay=' + test_overlay

        urls.append({'local_url': local_url, 'live_url': live_url, 'local_url_png': local_url_png,
                     'live_url_png': live_url_png})

    num_pages = -(-count // charts_per_page)


    next_page_url = None
    if test_page < num_pages:
        next_page_params = request.GET.copy()
        next_page_params['page'] = test_page+1
        next_page_url = request.build_absolute_uri('/grapher/admin/testall') + "?" + urllib.parse.urlencode(next_page_params)

    prev_page_url = None
    if test_page > 1:
        prev_page_params = request.GET.copy()
        prev_page_params['page'] = test_page-1
        prev_page_url = request.build_absolute_uri('/grapher/admin/testall') + "?" + urllib.parse.urlencode(prev_page_params)

    starting_point = (test_page - 1) * charts_per_page
    end_point = ((test_page - 1) * charts_per_page) + charts_per_page
    links = urls[starting_point:end_point]

    return render(request, 'testall.html', context={'urls': links, 'next_page_url': next_page_url,
                                                            'prev_page_url': prev_page_url, 'compare': test_compare,
                                                    })

def testsome(request):
    ids = [563, 646, 292, 51, 72, 132, 144, 194, 197, 864, 190, 302, 1690]
    charts = sorted(Chart.objects.filter(id__in=ids), key=lambda c: ids.index(c.id))

    urls = []
    for chart in charts:
        configfile = chart.config
        configfile['id'] = chart.id

        local_url = request.build_absolute_uri('/grapher/') + chart.config['slug']
        live_url = "https://ourworldindata.org/grapher/" + chart.config['slug']
        local_url_png = local_url + '.png'
        live_url_png = live_url + '.png'

        urls.append({'local_url': local_url, 'live_url': live_url, 'local_url_png': local_url_png,
                     'live_url_png': live_url_png})

    return render(request, 'testsome.html', context={'urls': urls})

def custom_login(request: HttpRequest):
    """
    Redirects to index page if the user is already logged in
    :param request: Request object
    :return: Redirects to index page if the user is logged in, otherwise will show the login page
    """
    if request.user.is_authenticated():
        return HttpResponseRedirect(reverse('listcharts'))
    else:
        return loginview(request)


def chartsjson(request: HttpRequest):
    limit = int(request.GET.get('limit') or 10000)
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT 
                id, 
                JSON_UNQUOTE(JSON_EXTRACT(config, "$.title")) AS title,
                JSON_UNQUOTE(JSON_EXTRACT(config, "$.slug")) AS slug,
                JSON_UNQUOTE(JSON_EXTRACT(config, "$.type")) AS type,
                JSON_UNQUOTE(JSON_EXTRACT(config, "$.internalNotes")) AS internalNotes,
                JSON_UNQUOTE(JSON_EXTRACT(config, "$.isPublished")) AS isPublished,
                JSON_UNQUOTE(JSON_EXTRACT(config, "$.tab")) AS tab,
                JSON_EXTRACT(config, "$.hasChartTab") = true AS hasChartTab,
                JSON_EXTRACT(config, "$.hasMapTab") = true AS hasMapTab,
                starred AS isStarred,
                last_edited_at AS lastEditedAt,
                last_edited_by AS lastEditedBy,
                published_at AS publishedAt,
                published_by AS publishedBy
            FROM charts ORDER BY last_edited_at DESC LIMIT %s
        """, [limit])

        charts = dictfetchall(cursor)

        varIds = [chart['id'] for chart in charts]

        cursor.execute("""
            SELECT dims.chartId, v.id as variableId, v.name as variableName FROM chart_dimensions AS dims JOIN variables AS v ON v.id=dims.variableId WHERE dims.chartId IN %s
        """, [varIds])

        variables={}
        for row in cursor.fetchall():
            chartId = row[0]
            variables[chartId] = variables.get(chartId, [])
            variables[chartId].append({ 'id': row[1], 'name': row[2] })

        for chart in charts:
            chart['variables'] = variables.get(chart['id'], [])

    return JsonResponse({
        'charts': charts,
        'numTotalCharts': Chart.objects.count()
    })

def listcharts(request: HttpRequest):
    return chart_editor(request)


def storechart(request: HttpRequest):
    if request.method == 'POST':
        chart = Chart()
        data = json.loads(request.body.decode('utf-8'))
        return savechart(request, chart, data, request.user)
    else:
        return HttpResponseRedirect(reverse('listcharts'))

def chart_editor(request: HttpRequest):
    # We cache the editor data based on the timestamp of the last database update
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT MAX(`MAX(updated_at)`)
            FROM (
                SELECT MAX(updated_at) from variables
                UNION SELECT MAX(updated_at) from sources
                UNION SELECT MAX(updated_at) from datasets
            ) AS timestamps
        """)
        database_updated_at = cursor.fetchone()[0]

    cachetag = str(int(database_updated_at.timestamp()))

    return render(request, 'admin.edit_chart.html', context={
        'current_user': request.user.name,
        'cachetag': cachetag
    })

def createchart(request: HttpRequest):
    return chart_editor(request)#, { "yAxis": { "min": 0 }})

def editchart(request: HttpRequest, chartid: Union[str, int]):
    return chart_editor(request)

def config_json_by_id(request, chartid):
    """
    :param request: Request object
    :param id: Chart id
    :return: config json
    """

    try:
        chart = Chart.objects.get(pk=int(chartid))
    except Chart.DoesNotExist:
        return HttpResponseNotFound('Invalid chart id!')

    chart.config['id'] = chart.id
    return JsonResponse(chart.config)

def namespacedata(request: HttpRequest, namespace: str, cachetag: Optional[str]):
    datasets = []

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT v.name, v.id, d.name as datasetName, d.namespace FROM variables as v JOIN datasets as d ON v.datasetId = d.id WHERE namespace=%s ORDER BY d.updated_at DESC
        """, [namespace])

        dataset = None
        for row in dictfetchall(cursor):
            if dataset is None or row['datasetName'] != dataset['name']:
                if dataset is not None:
                    datasets.append(dataset)
                dataset = {
                    'name': row['datasetName'],
                    'namespace': row['namespace'],
                    'variables': []
                }

            dataset['variables'].append({
                'id': row['id'],
                'name': row['name']
            })

        if dataset is not None:
            datasets.append(dataset)

    response = JsonResponse({
        'datasets': datasets
    })

    if cachetag:
        response['Cache-Control'] = 'public, max-age=31536000'

    return response


def editordata(request: HttpRequest, cachetag: Optional[str]):
    namespaces = list(Dataset.objects.values_list('namespace', flat=True).distinct())

    response = JsonResponse({
        'namespaces': namespaces
    })

    if cachetag:
        response['Cache-Control'] = 'public, max-age=31536000'

    return response

def _post_update(request, chart: Chart):
    # Bake published charts into static build
    Chart.bake(request.user, chart.config['slug'])

    return JsonResponse({'success': True, 'data': {'id': chart.pk}})

def savechart(request, chart: Chart, data: Dict, user: User):
    with transaction.atomic():
        wasPublished = data.get("isPublished") or chart.config.get("isPublished")
        isExisting = chart.id != None

        if data.get('isPublished'):
            if not data.get('slug') or not data.get('slug').strip():
                return JsonErrorResponse(f"Invalid chart slug `{data.get('slug')}`")
            elif ChartSlugRedirect.objects.filter(~Q(chart_id=chart.pk)).filter(Q(slug=data['slug'])):
                return JsonErrorResponse("This chart slug was previously used by another chart: %s" % data["slug"])
            elif Chart.objects.filter(~Q(pk=chart.pk)).filter(config__slug=data['slug'], config__isPublished=True):
                return JsonErrorResponse("This chart slug is currently in use by another chart: %s" % data["slug"])
            elif chart.config.get('isPublished') and chart.config.get('slug') and chart.config.get('slug') != data['slug']:
                # Changing the slug of an already published chart-- create a redirect
                try:
                    old_chart_redirect = ChartSlugRedirect.objects.get(slug=chart.config['slug'])
                    old_chart_redirect.chart_id = chart.pk
                    old_chart_redirect.save()
                except ChartSlugRedirect.DoesNotExist:
                    new_chart_redirect = ChartSlugRedirect()
                    new_chart_redirect.chart_id = chart.pk
                    new_chart_redirect.slug = chart.config['slug']
                    new_chart_redirect.save()

        dims = []

        # Set publication info if publishing chart
        if data.get('isPublished') and not chart.config.get('isPublished'):
            chart.published_at = timezone.now()
            chart.published_by = user

        data['version'] = chart.config.get('version', 0) + 1
        chart.config = data
        chart.last_edited_at = timezone.now()
        chart.last_edited_by = user
        chart.save()

        for each in ChartDimension.objects.filter(chartId=chart.pk):
            each.delete()

        for i, dim in enumerate(data["dimensions"]):
            variable = Variable.objects.get(id=dim["variableId"])

            newdim = ChartDimension()
            newdim.chartId = chart
            newdim.variableId = variable
            newdim.property = dim.get('property', None)
            newdim.order = i
            newdim.save()

            if dim.get('saveToVariable'):
                for key in dim['display']:
                    variable.display[key] = dim['display'][key]
                variable.save()

    if wasPublished:
        _post_update(request, chart)

    return JsonResponse({ 'success': True, 'data': {'id': chart.pk} })

def managechart(request: HttpRequest, chartid: str):
    try:
        chart = Chart.objects.get(pk=int(chartid))
    except Chart.DoesNotExist:
        return HttpResponseNotFound('No such chart!')
    except ValueError:
        return HttpResponseNotFound('No such chart!')
    if request.method == 'PUT':
        data = json.loads(request.body.decode('utf-8'))
        return savechart(request, chart, data, request.user)
    if request.method == 'DELETE':
        wasPublished = chart.config.get("isPublished")
        chart.delete()
        if wasPublished:
            _post_update(request, chart)
        return JsonResponse({ 'success': True })
    if request.method == 'GET':
        return HttpResponseRedirect(reverse('showchartinternal', args=(chartid,)))

def starchart(request: HttpRequest, chartid: str):
    with transaction.atomic():
        try:
            chart = Chart.objects.get(pk=int(chartid))
        except Chart.DoesNotExist:
            return JsonErrorResponse('No such chart!', status=404)
        except ValueError:
            return JsonErrorResponse('No such chart!', status=404)

        Chart.objects.update(starred=False)
        chart.starred = True
        chart.save()

    Chart.bake(request.user, chart.config['slug'])

    return JsonResponse({'success': True})

def importdata(request: HttpRequest):
    datasets = Dataset.objects.filter(namespace='owid').order_by('name').values()
    datasetlist = []
    for each in datasets:
        each['subcategoryId'] = each['subcategoryId_id'] # XXX
        each['created_at'] = str(each['created_at'])
        each['updated_at'] = str(each['updated_at'])
        datasetlist.append(each)

    vartypes = Variable.objects.values()
    vartypeslist = []
    for each in vartypes:
        each['created_at'] = str(each['created_at'])
        each['updated_at'] = str(each['updated_at'])
        each['uploaded_at'] = str(each['uploaded_at'])
        vartypeslist.append(each)
    # we probably don't need SourceTemplate anymore
    source_template = dict(Setting.objects.filter(meta_name='sourceTemplate').values().first())
    source_template['created_at'] = str(source_template['created_at'])
    source_template['updated_at'] = str(source_template['updated_at'])

    categories = DatasetSubcategory.objects.all().select_related().filter(
        categoryId__fetcher_autocreated=False).order_by('categoryId__pk').order_by('pk')
    category_list = []
    for each in categories:
        category_list.append({'name': each.name, 'id': each.pk, 'parent': each.categoryId.name})
    entitynames = Entity.objects.all().iterator()
    entitynameslist = []
    entitycodeslist = []
    for each in entitynames:
        entitynameslist.append(each.name)
        entitycodeslist.append(each.code)
    all_entitynames = entitynameslist + entitycodeslist

    data = {'datasets': datasetlist, 'categories': category_list, 'varTypes': vartypeslist, 'sourceTemplate': source_template,
            'entityNames': all_entitynames}

    if '.json' in urlparse(request.get_full_path()).path:
        return JsonResponse(data, safe=False)
    else:
        return render(request, 'admin.importer.html', context={'current_user': request.user.name,
                                                               'importerdata': json.dumps(data)})


def store_import_data(request: HttpRequest):
    if request.method == 'POST':
        try:
            with transaction.atomic():
                data = json.loads(request.body.decode('utf-8'))
                datasetmeta = data['dataset']
                entities = data['entities']
                entitynames = data['entityNames']
                years = data['years']
                variables = data['variables']

                datasetprops = {'name': datasetmeta['name'],
                                'description': datasetmeta['description'],
                                'categoryId': DatasetSubcategory.objects.get(pk=datasetmeta['subcategoryId']).categoryId,
                                'subcategoryId': DatasetSubcategory.objects.get(pk=datasetmeta['subcategoryId'])
                                }

                if datasetmeta['id']:
                    dataset = Dataset.objects.get(pk=datasetmeta['id'])
                    dataset_old_name = dataset.name  # needed for version tracking csv export
                    Dataset.objects.filter(pk=datasetmeta['id']).update(updated_at=timezone.now(), **datasetprops)
                else:
                    dataset = Dataset(**datasetprops)
                    dataset_old_name = None
                    dataset.save()

                dataset_id = dataset.pk

                codes = Entity.objects.filter(validated=True).values('name', 'code')

                codes_dict = {}

                for each in codes:
                    codes_dict[each['code']] = each['name']

                entitynames_list = Entity.objects.values_list('name', flat=True)

                for i in range(0, len(entitynames)):
                    name = entitynames[i]
                    if codes_dict.get(name, 0):
                        entitynames[i] = codes_dict[name]

                entitynames_to_insert = []

                for each in entitynames:
                    if each not in entitynames_list:
                        entitynames_to_insert.append(each)

                alist = [Entity(name=val, validated=False) for val in entitynames_to_insert]

                Entity.objects.bulk_create(alist)

                codes = Entity.objects.values('name', 'id')

                entitiy_name_to_id = {}

                for each in codes:
                    entitiy_name_to_id[each['name']] = each['id']

                source_ids_by_name: Dict[str, str] = {}

                for variable in variables:
                    source_name = variable['source']['name']
                    if source_ids_by_name.get(source_name, 0):
                        source_id = source_ids_by_name[source_name]
                    else:
                        if variable['source']['id']:
                            source_id = variable['source']['id']
                        else:
                            source_id = None
                        source_desc = {
                            'dataPublishedBy': None if not variable['source']['dataPublishedBy'] else variable['source']['dataPublishedBy'],
                            'dataPublisherSource': None if not variable['source']['dataPublisherSource'] else variable['source']['dataPublisherSource'],
                            'link': None if not variable['source']['link'] else variable['source']['link'],
                            'retrievedDate': None if not variable['source']['retrievedDate'] else variable['source']['retrievedDate'],
                            'additionalInfo': None if not variable['source']['additionalInfo'] else variable['source']['additionalInfo']
                        }
                        if source_id:
                            existing_source = Source.objects.get(pk=source_id)
                            existing_source.name = variable['source']['name']
                            existing_source.updated_at = timezone.now()
                            existing_source.description = json.dumps(source_desc)
                            existing_source.save()
                        else:
                            new_source = Source(datasetId=dataset_id, name=source_name, description=json.dumps(source_desc))
                            new_source.save()
                            source_id = new_source.pk
                            source_ids_by_name[source_name] = source_id

                    values = variable['values']
                    variableprops = {'name': variable['name'], 'description': variable['description'], 'unit': variable['unit'],
                                     'coverage': variable['coverage'], 'timespan': variable['timespan'],
                                     'variableTypeId': VariableType.objects.get(pk=3),
                                     'datasetId': Dataset.objects.get(pk=dataset_id),
                                     'sourceId': Source.objects.get(pk=source_id),
                                     'uploaded_at': timezone.now(),
                                     'updated_at': timezone.now(),
                                     'uploaded_by': request.user
                                     }
                    if variable['overwriteId']:
                        Variable.objects.filter(pk=variable['overwriteId']).update(**variableprops)
                        varid = variable['overwriteId']
                    else:
                        varid = Variable(**variableprops)
                        varid.save()
                        varid = varid.pk
                    while DataValue.objects.filter(variableId__pk=varid).first():
                        with connection.cursor() as c:
                            c.execute('DELETE FROM %s WHERE variableId = %s LIMIT 10000;' %
                                      (DataValue._meta.db_table, varid))
                            # the LIMIT is here so that the database doesn't try to delete a large number of values at
                            # once and becomes unresponsive

                    insert_string = 'INSERT into data_values (value, year, entityId, variableId) VALUES (%s, %s, %s, %s)'
                    data_values_tuple_list = []
                    for i in range(0, len(years)):
                        if values[i] == '':
                            continue
                        data_values_tuple_list.append((values[i], years[i],
                                                       entitiy_name_to_id[entitynames[entities[i]]],
                                                       varid))

                        if len(data_values_tuple_list) > 3000:  # insert when the length of the list goes over 3000
                            with connection.cursor() as dbconnection:
                                dbconnection.executemany(insert_string, data_values_tuple_list)
                            data_values_tuple_list = []

                    if len(data_values_tuple_list):  # insert any leftover data_values
                        with connection.cursor() as dbconnection:
                            dbconnection.executemany(insert_string, data_values_tuple_list)
                    with connection.cursor() as cursor:
                        cursor.execute("DELETE FROM sources WHERE sources.id NOT IN (SELECT variables.sourceId FROM variables)")

                write_dataset_csv(dataset.pk, datasetprops['name'],
                                  dataset_old_name, request.user.get_full_name(), request.user.email)

                return JsonResponse({'datasetId': dataset_id}, safe=False)
        except Exception as e:
            if len(e.args) > 1:
                error_m = str(e.args[0]) + ' ' + str(e.args[1])
            else:
                error_m = e.args[0]
            return HttpResponse(error_m, status=500)


def listdatasets(request: HttpRequest):
    variables = Variable.objects.filter(datasetId__namespace='owid').select_related('datasetId').order_by('-datasetId__updated_at')
    datasets: Dict = {}
    for each in variables:
        if each.uploaded_by:
            uploaded_by = each.uploaded_by.name
        else:
            uploaded_by = None
        if datasets.get(each.datasetId.pk, 0):
            datasets[each.datasetId.pk]['variables'].append({'name': each.name, 'id': each.pk,
                                                             'uploaded_at': str(each.uploaded_at),
                                                             'uploaded_by': uploaded_by})
        else:
            datasets[each.datasetId.pk] = {'name': each.datasetId.name, 'id': each.datasetId.pk, 'variables': [{'name': each.name,
                                                                                       'id': each.pk,
                                                                                       'uploaded_at': str(
                                                                                           each.uploaded_at),
                                                                                       'uploaded_by': uploaded_by
                                                                                       }]}
    dataset_list = []
    for value in sorted(datasets.keys(), reverse=True):
        dataset_list.append(datasets[value])
    return render(request, 'admin.datasets.html', context={'current_user': request.user.name,
                                                           'datasets': dataset_list})


def showdataset(request: HttpRequest, datasetid: str):
    try:
        dataset = Dataset.objects.get(pk=int(datasetid))
    except Dataset.DoesNotExist:
        return HttpResponseNotFound('Dataset does not exist!')

    dataset_dict = {'id': dataset.pk, 'name': dataset.name, 'category': dataset.categoryId.name,
                    'subcategory': dataset.subcategoryId.name,
                    'description': dataset.description}

    dataset_vars = Variable.objects.filter(datasetId=dataset)
    dataset_chartdims = ChartDimension.objects.filter(variableId__in=dataset_vars)
    dataset_chart_ids = []
    for each in dataset_chartdims:
        dataset_chart_ids.append(each.chartId.pk)
    dataset_charts = Chart.objects.filter(pk__in=dataset_chart_ids)
    return render(request, 'admin.datasets.show.html', context={'current_user': request.user.name,
                                                                'dataset': dataset_dict,
                                                                'variables': dataset_vars.values(),
                                                                'charts': dataset_charts,
                                                                })


def editdataset(request: HttpRequest, datasetid: str):
    try:
        dataset = Dataset.objects.get(pk=int(datasetid))
    except Dataset.DoesNotExist:
        return HttpResponseNotFound('Dataset does not exist!')

    sources_list = []
    sources = Source.objects.all().values('pk', 'name')
    for each in sources:
        sources_list.append({'id': int(each['pk']), 'name': each['name']})
    cats_list = []
    if dataset.namespace == 'owid':
        categories = DatasetCategory.objects.filter(fetcher_autocreated=False).values('pk', 'name')
    else:
        categories = DatasetCategory.objects.values('pk', 'name')
    for each in categories:
        cats_list.append({'id': int(each['pk']), 'name': each['name']})
    subcats_list = []
    if dataset.namespace == 'owid':
        subcategories = DatasetSubcategory.objects.filter(categoryId__fetcher_autocreated=False).values('pk', 'name')
    else:
        subcategories = DatasetSubcategory.objects.values('pk', 'name')
    for each in subcategories:
        subcats_list.append({'id': int(each['pk']), 'name': each['name']})
    return render(request, 'admin.datasets.edit.html', context={'current_user': request.user.name,
                                                                'dataset': dataset,
                                                                'sources': sources_list,
                                                                'categories': cats_list,
                                                                'subcategories': subcats_list,
                                                                })


def managedataset(request: HttpRequest, datasetid: str):
    try:
        dataset = Dataset.objects.get(pk=int(datasetid))
    except Dataset.DoesNotExist:
        return HttpResponseNotFound('Dataset does not exist!')

    if request.method == 'POST':
        request_dict = QueryDict(request.body.decode('utf-8')).dict()
        if request_dict['_method'] == 'DELETE':
            try:
                dataset.delete()
            except Exception as e:
                if e.args[0] == 1451:
                    messages.error(request, 'Dataset cannot be deleted while a chart still needs it. Delete charts or change their variables first.')
                    return HttpResponseRedirect(reverse('showdataset', args=[datasetid]))
                else:
                    messages.error(request, e.args[1])
                    return HttpResponseRedirect(reverse('showdataset', args=[datasetid]))
            messages.success(request, 'Dataset deleted.')
            return HttpResponseRedirect(reverse('listdatasets'))
        if request_dict['_method'] == 'PATCH':
            dataset_old_name = dataset.name
            request_dict.pop('_method', None)
            request_dict.pop('csrfmiddlewaretoken', None)
            request_dict['categoryId'] = DatasetCategory.objects.get(pk=request_dict['categoryId'])
            request_dict['subcategoryId'] = DatasetSubcategory.objects.get(pk=request_dict['subcategoryId'])
            Dataset.objects.filter(pk=datasetid).update(updated_at=timezone.now(), **request_dict)
            write_dataset_csv(dataset.pk, request_dict['name'],
                              dataset_old_name, request.user.get_full_name(), request.user.email)
            messages.success(request, 'Dataset updated!')
            return HttpResponseRedirect(reverse('showdataset', args=[datasetid]))

    if request.method == 'GET':
        return HttpResponseRedirect(reverse('showdataset', args=[datasetid]))


def dataset_csv(request: HttpRequest, datasetid: str):
    try:
        dataset = Dataset.objects.get(pk=int(datasetid))
    except Dataset.DoesNotExist:
        return HttpResponseNotFound('Dataset does not exist!')
    # all variables that belong to a dataset being downloaded
    allvariables = Variable.objects.filter(datasetId=dataset)

    datasetvarlist = []

    for var in allvariables.values('id', 'name'):
        datasetvarlist.append({'id': var['id'], 'name': var['name']})

    datasetvarlist = sorted(datasetvarlist, key=lambda k: k['id'])

    id_tuple = ''
    varlist = []
    headerlist = ['Entity', 'Year']

    for each in datasetvarlist:
        id_tuple += str(each['id']) + ','
        headerlist.append(each['name'])
        varlist.append(each['id'])
    # removing that last comma
    id_tuple = id_tuple[:-1]
    # get all entity ids that have data values in the current dataset
    with connection.cursor() as entity_cursor:
        entity_cursor.execute('select distinct entityId from data_values where variableId in (%s);' % ','.join([str(item['id']) for item in allvariables.values('id')]))
        dataset_entities_list = [item[0] for item in entity_cursor.fetchall()]
    # get the names for entity ids
    allentities = Entity.objects.filter(pk__in=dataset_entities_list).values('id', 'name')
    allentities_dict = {item['id']: item['name'] for item in allentities}
    allentities_list = []
    for each in allentities:
        allentities_list.append({'id': each['id'], 'name': each['name']})
    allentities_list = sorted(allentities_list, key=lambda k: k['name'])
    allentities_list = [item['id'] for item in allentities_list]
    # we are splitting the entities to groups of 3 for querying
    # this is still not very efficient, but better than querying values for each entity separately
    # if we don't set this limitation and query for values from all entities, th db usually hangs -
    # - when there are many rows present
    entity_chunks = [allentities_list[x:x + 3] for x in range(0, len(allentities_list), 3)]

    sql_query = 'SELECT `value`, `year`, data_values.`variableId` as var_id, data_values.`entityId` as entity_id ' \
                ' from data_values ' \
                ' WHERE ' \
                'data_values.`variableId` in (%s) AND data_values.`entityId` in (%s) ORDER BY entityId, year, variableId;'

    # our csv streaming function

    def stream():
        buffer_ = StringIO()
        writer = csv.writer(buffer_)
        writer.writerow(headerlist)
        current_row = None

        for chunk in entity_chunks:
            with connection.cursor() as outer_cursor:
                outer_cursor.execute(sql_query % (id_tuple, ','.join([str(x) for x in chunk])))
                rows = outer_cursor.fetchall()
            for row in rows:
                if not current_row or current_row[0] != row[3] or current_row[1] != row[1]:
                    if current_row:
                        current_row[0] = allentities_dict[current_row[0]]
                        writer.writerow(current_row)
                        buffer_.seek(0)
                        data = buffer_.read()
                        buffer_ = StringIO()
                        writer = csv.writer(buffer_)
                        yield data

                    current_row = [row[3], row[1]]
                    for i in range(0, len(varlist)):
                        current_row.append("")

                theindex = 2 + varlist.index(row[2])
                current_row[theindex] = row[0]

        current_row[0] = allentities_dict[current_row[0]]
        writer.writerow(current_row)
        buffer_.seek(0)
        data = buffer_.read()
        yield data

    response = StreamingHttpResponse(
        stream(), content_type='text/csv'
    )
    ascii_filename = unidecode(dataset.name)
    disposition = "attachment; filename='%s.csv'" % ascii_filename
    response['Content-Disposition'] = disposition
    response['Cache-Control'] = 'public, max-age=0, s-maxage=604800'
    return response


def dataset_json(request: HttpRequest, datasetid: str):
    try:
        dataset = Dataset.objects.get(pk=int(datasetid))
    except Dataset.DoesNotExist:
        return HttpResponseNotFound('Dataset does not exist!')

    data = {'name': dataset.name, 'description': dataset.description, 'categoryId': dataset.categoryId_id,
            'subcategoryId': dataset.subcategoryId_id, 'variables': []}

    allchart_dimensions = ChartDimension.objects.all().values('chartId', 'variableId')
    var_to_chart = {}
    for each in allchart_dimensions:
        if var_to_chart.get(each['variableId'], 0):
            var_to_chart[each['variableId']].append(each['chartId'])
        else:
            var_to_chart[each['variableId']] = []
            var_to_chart[each['variableId']].append(each['chartId'])

    variables = Variable.objects.filter(datasetId=dataset.id).select_related('sourceId')

    for var in variables:
        source_description = json.loads(var.sourceId.description)
        sourcedata = {
            'id': var.sourceId.pk,
            'name': var.sourceId.name,
            'dataPublishedBy': "" if not source_description['dataPublishedBy'] else source_description['dataPublishedBy'],
            'dataPublisherSource': "" if not source_description['dataPublisherSource'] else source_description['dataPublisherSource'],
            'link': "" if not source_description['link'] else source_description['link'],
            'retrievedDate': "" if not source_description['retrievedDate'] else source_description['retrievedDate'],
            'additionalInfo': "" if not source_description['additionalInfo'] else source_description['additionalInfo']
        }

        chartdata = []

        for onechart in var_to_chart.get(var.pk, []):
            chart = Chart.objects.get(pk=onechart)
            chartdata.append({
                'id': chart.pk,
                'name': chart.config['title']
            })

        vardata = {
            'id': var.pk,
            'name': var.name,
            'unit': var.unit,
            'description': var.description,
            'coverage': var.coverage,
            'timespan': var.timespan,
            'source': sourcedata,
            'charts': chartdata
        }

        data['variables'].append(vardata)

    return JsonResponse(data, safe=False)


def check_invitation_statuses():
    invites = UserInvitation.objects.filter(status='pending')
    for each in invites:
        if each.valid_till <= timezone.now():
            each.status = 'expired'
            each.save()


def listcategories(request: HttpRequest):
    categories = DatasetCategory.objects.values()
    return render(request, 'admin.categories.html', context={'current_user': request.user.name,
                                                             'categories': categories
                                                             })


def showcategory(request: HttpRequest, catid: str):
    try:
        catobj = DatasetCategory.objects.get(pk=int(catid))
    except DatasetCategory.DoesNotExist:
        return HttpResponseNotFound('Category does not exist!')

    subcategories = DatasetSubcategory.objects.filter(categoryId=catobj).values()
    category = DatasetCategory.objects.filter(pk=int(catid)).values()[0]
    category['subcategories'] = subcategories

    return render(request, 'admin.categories.show.html', context={'current_user': request.user.name,
                                                                  'category': category
                                                                  })


def managecategory(request: HttpRequest, catid: str):
    try:
        category = DatasetCategory.objects.get(pk=int(catid))
    except DatasetCategory.DoesNotExist:
        return HttpResponseNotFound('Category does not exist!')

    if request.method == 'POST':
        request_dict = QueryDict(request.body.decode('utf-8')).dict()
        if request_dict['_method'] == 'PATCH':
            request_dict.pop('_method', None)
            request_dict.pop('csrfmiddlewaretoken', None)
            DatasetCategory.objects.filter(pk=catid).update(updated_at=timezone.now(), **request_dict)
            messages.success(request, 'Category updated!')
            return HttpResponseRedirect(reverse('showcategory', args=[catid]))
        if request_dict['_method'] == 'DELETE':
            request_dict.pop('_method', None)
            request_dict.pop('csrfmiddlewaretoken', None)
            category = DatasetCategory.objects.get(pk=int(catid))
            subcategory = DatasetSubcategory.objects.filter(categoryId=category)
            try:
                for each in subcategory:
                    each.delete()
                category.delete()
            except Exception as e:
                messages.error(request, e.args[1])
                return HttpResponseRedirect(reverse('editcategory', args=[catid]))
            messages.success(request, 'Category deleted!')
            return HttpResponseRedirect(reverse('listcategories'))


def editcategory(request: HttpRequest, catid: str):
    try:
        category = DatasetCategory.objects.get(pk=int(catid))
    except DatasetCategory.DoesNotExist:
        return HttpResponseNotFound('Category does not exist!')
    category = DatasetCategory.objects.filter(pk=int(catid)).values()[0]
    return render(request, 'admin.categories.edit.html', context={'current_user': request.user.name,
                                                                  'category': category
                                                                  })


def listvariables(request: HttpRequest):
    variables = Variable.objects.values()

    return render(request, 'admin.variables.html', context={'current_user': request.user.name,
                                                            'variables': variables
                                                            })


def showvariable(request: HttpRequest, variableid: str):
    try:
        variable = Variable.objects.get(pk=int(variableid))
    except Variable.DoesNotExist:
        return HttpResponseNotFound('Variable does not exist!')

    items_per_page = 50

    chart_dims = list(ChartDimension.objects.filter(variableId=variable).values('chartId'))
    chart_id_list = []
    for each in chart_dims:
        chart_id_list.append(each['chartId'])
    charts = list(Chart.objects.filter(pk__in=chart_id_list))

    variable_dict = {}
    variable_dict['name'] = variable.name
    variable_dict['id'] = variable.pk
    variable_dict['unit'] = variable.unit
    variable_dict['short_unit'] = variable.short_unit
    variable_dict['description'] = variable.description
    variable_dict['dataset'] = {'name': variable.datasetId.name, 'id': variable.datasetId.pk}
    variable_dict['source'] = {'name': variable.sourceId.name, 'id': variable.sourceId.pk}
    variable_dict['charts'] = charts

    request_dict = get_query_as_dict(request)
    if request_dict.get('search', [0])[0]:
        value_query = request_dict.get('value', [''])[0]
        year_query = request_dict.get('year', [None])[0]
        entity_query = request_dict.get('name', [''])[0]
        try:
            year_query = int(year_query)
        except ValueError:
            year_query = 0
        except TypeError:
            year_query = 0

        values = DataValue.objects.filter(variableId=variable)
        if value_query:
            values = values.filter(value=value_query)
        if year_query:
            values = values.filter(year=year_query)
        if entity_query:
            values = values.filter(entityId__name=entity_query)

    else:
        values = DataValue.objects.filter(variableId=variable)

    values = list(values.values('pk', 'value', 'year', 'entityId__name'))

    total_rows = len(values)
    total_pages = -(-len(values) // items_per_page)
    page_number = get_query_as_dict(request).get('page', [0])

    try:
        page_number = int(page_number[0])
    except ValueError:
        page_number = 0

    if page_number > 1:
        vals = values[(page_number - 1) * items_per_page:page_number * items_per_page]
    else:
        vals = values[:items_per_page]

    if vals:
        if total_pages >= 13:
            if page_number < 7:
                nav_pages = [1, 2, 3, 4, 5, 6, 7, 8, '#', total_pages - 1, total_pages]
            elif page_number > total_pages - 5:
                nav_pages = [1, 2, '#', total_pages - 7, total_pages - 6, total_pages - 5, total_pages - 4,
                             total_pages - 3,
                             total_pages - 2, total_pages - 1, total_pages]
            else:
                nav_pages = [1, 2, '#', page_number - 3, page_number - 2, page_number - 1, page_number,
                             page_number + 1,
                             page_number + 2, page_number + 3, '#', total_pages - 1, total_pages]
        else:
            nav_pages = [n for n in range(1, total_pages + 1)]
    else:
        nav_pages = []

    variable_dict['values'] = vals

    allentities = []
    for each in values:
        if each['entityId__name'] not in allentities:
            allentities.append(each['entityId__name'])

    request_string_for_pages = '?'
    for key, value in request_dict.items():
        if key != 'page':
            request_string_for_pages += key + '=' + value[0] + '&'

    return render(request, 'admin.variables.show.html', context={'current_user': request.user.name,
                                                                 'variable': variable_dict,
                                                                 'nav_pages': nav_pages,
                                                                 'current_page': page_number,
                                                                 'total_rows': total_rows,
                                                                 'entities': allentities,
                                                                 'page_request_string': request_string_for_pages
                                                                 })


def editvariable(request: HttpRequest, variableid: str):
    try:
        variable = Variable.objects.get(pk=int(variableid))
    except Variable.DoesNotExist:
        return HttpResponseNotFound('Variable does not exist!')

    variable_dict = {
        'name': variable.name,
        'id': variable.pk,
        'unit': variable.unit,
        'short_unit': variable.short_unit if variable.short_unit else '',
        'coverage': variable.coverage,
        'timespan': variable.timespan,
        'description': variable.description,
        'source': {'id': variable.sourceId.pk, 'name': variable.sourceId.name}
    }

    return render(request, 'admin.variables.edit.html', context={'current_user': request.user.name,
                                                                 'variable': variable_dict
                                                                 })


def managevariable(request: HttpRequest, variableid: str):
    try:
        variable = Variable.objects.get(pk=int(variableid))
    except Variable.DoesNotExist:
        return HttpResponseNotFound('Variable does not exist!')

    if request.method == 'POST':
        request_dict = QueryDict(request.body.decode('utf-8')).dict()
        if request_dict['_method'] == 'DELETE':
            try:
                variable.delete()
            except Exception as e:
                messages.error(request, e.args[1])
                return HttpResponseRedirect(reverse('showvariable', args=[variableid]))
            messages.success(request, 'Variable deleted.')
            return HttpResponseRedirect(reverse('listvariables'))
        if request_dict['_method'] == 'PATCH':
            request_dict.pop('_method', None)
            request_dict.pop('csrfmiddlewaretoken', None)
            request_dict.pop('sourceId', None)
            request_dict['short_unit'] = None if not request_dict['short_unit'].strip() else request_dict['short_unit'].strip()
            Variable.objects.filter(pk=int(variableid)).update(updated_at=timezone.now(), **request_dict)
            messages.success(request, 'Variable updated!')
            return HttpResponseRedirect(reverse('showvariable', args=[variableid]))

    if request.method == 'GET':
        return HttpResponseRedirect(reverse('showvariable', args=[variableid]))


def listlicenses(request: HttpRequest):
    licenses = License.objects.values()
    return render(request, 'admin.licenses.html', context={'current_user': request.user.name,
                                                           'licenses': licenses
                                                           })


def showlicense(request: HttpRequest, licenseid: str):
    try:
        license = License.objects.get(pk=int(licenseid))
    except License.DoesNotExist:
        return HttpResponseNotFound('License does not exist!')

    return render(request, 'admin.licenses.show.html', context={'current_user': request.user.name,
                                                                'license': license
                                                                })


def editlicense(request: HttpRequest, licenseid: str):
    try:
        license = License.objects.get(pk=int(licenseid))
    except License.DoesNotExist:
        return HttpResponseNotFound('License does not exist!')

    license = {
        'id': license.pk,
        'name': license.name,
        'description': license.description
    }

    return render(request, 'admin.licenses.edit.html', context={'current_user': request.user.name,
                                                                'license': license
                                                                })


def managelicense(request: HttpRequest, licenseid: str):
    try:
        license = License.objects.get(pk=int(licenseid))
    except License.DoesNotExist:
        return HttpResponseNotFound('License does not exist!')

    if request.method == 'POST':
        request_dict = QueryDict(request.body.decode('utf-8')).dict()
        if request_dict['_method'] == 'PATCH':
            request_dict.pop('_method', None)
            request_dict.pop('csrfmiddlewaretoken', None)
            License.objects.filter(pk=int(licenseid)).update(updated_at=timezone.now(), **request_dict)
            messages.success(request, 'License updated!')
            return HttpResponseRedirect(reverse('showlicense', args=[licenseid]))

    if request.method == 'GET':
        return HttpResponseRedirect(reverse('showlicense', args=[licenseid]))

def _showsource(request: HttpRequest, sourceid: str):
    try:
        source = Source.objects.get(pk=int(sourceid))
    except Source.DoesNotExist:
        return HttpResponseNotFound('Source does not exist!')

    source = {'id': source.pk, 'name': source.name, 'description': json.loads(source.description), 'datasetId': source.datasetId}

    try:
        dataset = Dataset.objects.get(pk=source['datasetId'])
        source['dataset'] = {'id': dataset.pk, 'name': dataset.name}
    except:
        source['dataset'] = None

    variables = Variable.objects.filter(sourceId__pk=source['id']).values()

    source['variables'] = variables

    return render(request, 'admin.sources.show.html', context={'current_user': request.user.name,
                                                               'source': source})


def editsource(request: HttpRequest, sourceid: str):
    try:
        source = Source.objects.get(pk=int(sourceid))
    except Source.DoesNotExist:
        return HttpResponseNotFound('Source does not exist!')
    description = json.loads(source.description)
    source = {
        'id': source.pk,
        'name': source.name,
        'dataPublishedBy': "" if not description['dataPublishedBy'] else description['dataPublishedBy'],
        'dataPublisherSource': "" if not description['dataPublisherSource'] else description['dataPublisherSource'],
        'link': "" if not description['link'] else description['link'],
        'retrievedDate': "" if not description['retrievedDate'] else description['retrievedDate'],
        'additionalInfo': "" if not description['additionalInfo'] else description['additionalInfo']
    }

    return render(request, 'admin.sources.edit.html', context={'current_user': request.user.name,
                                                               'source': source})


def managesource(request: HttpRequest, sourceid: str):
    if request.method == 'POST':
        try:
            source = Source.objects.get(pk=int(sourceid))
        except Source.DoesNotExist:
            return HttpResponseNotFound('Source does not exist!')

        request_dict = QueryDict(request.body.decode('utf-8')).dict()
        if request_dict['_method'] == 'PATCH':
            request_dict.pop('_method', None)
            request_dict.pop('csrfmiddlewaretoken', None)
            for dictkey, value in request_dict.items():
                if not value.strip():
                    request_dict[dictkey] = None
            description = {
                'dataPublishedBy': request_dict['dataPublishedBy'],
                'dataPublisherSource': request_dict['dataPublisherSource'],
                'link': request_dict['link'],
                'retrievedDate': request_dict['retrievedDate'],
                'additionalInfo': request_dict['additionalInfo']
            }
            try:
                source.name = request_dict['name']
                source.updated_at = timezone.now()
                source.description = json.dumps(description)
                source.save()
            except Exception as e:
                messages.error(request, e.args[1])
                return HttpResponseRedirect(reverse('managesource', args=[sourceid]))
            messages.success(request, 'Source updated!')
            return HttpResponseRedirect(reverse('managesource', args=[sourceid]))

    if request.method == 'GET':
        return _showsource(request, sourceid)


def editsubcategory(request: HttpRequest, subcatid: str):
    try:
        subcat = DatasetSubcategory.objects.get(pk=int(subcatid))
    except DatasetSubcategory.DoesNotExist:
        return HttpResponseNotFound('Subcategory does not exist!')

    subcategory = {'id': subcat.pk, 'name': subcat.name, 'category': subcat.categoryId.pk}
    categories = DatasetCategory.objects.values()
    category = {'id': subcat.categoryId.pk}

    return render(request, 'admin.subcategories.edit.html', context={'current_user': request.user.name,
                                                                     'subcategory': subcategory,
                                                                     'categories': categories,
                                                                     'category': category})


def managesubcategory(request: HttpRequest, subcatid: str):
    try:
        subcat = DatasetSubcategory.objects.get(pk=int(subcatid))
    except DatasetSubcategory.DoesNotExist:
        return HttpResponseNotFound('Subcategory does not exist!')

    parent_cat = subcat.categoryId.pk

    if request.method == 'POST':
        request_dict = QueryDict(request.body.decode('utf-8')).dict()
        if request_dict['_method'] == 'DELETE':
            try:
                subcat.delete()
            except Exception as e:
                messages.error(request, e.args[1])
                return HttpResponseRedirect(reverse('editsubcategory', args=[subcatid]))
            messages.success(request, 'Subcategory deleted.')
            return HttpResponseRedirect(reverse('showcategory', args=[parent_cat]))
        if request_dict['_method'] == 'PATCH':
            request_dict.pop('_method', None)
            request_dict.pop('csrfmiddlewaretoken', None)
            try:
                DatasetSubcategory.objects.filter(pk=int(subcatid)).update(updated_at=timezone.now(), **request_dict)
            except Exception as e:
                messages.error(request, e.args[1])
                return HttpResponseRedirect(reverse('editsubcategory', args=[subcatid]))
            messages.success(request, 'Subcategory updated!')
            return HttpResponseRedirect(reverse('showcategory', args=[parent_cat]))


def createsubcategory(request: HttpRequest):
    categories = DatasetCategory.objects.values()
    return render(request, 'admin.subcategories.create.html',context={'current_user': request.user.name,
                                                                      'categories': categories
                                                                      })


def storesubcategory(request: HttpRequest):
    categories = DatasetCategory.objects.values()
    if request.method == 'POST':
        if not request.POST.get('name', 0):
            messages.error(request, 'Name field should not be empty.')
        if messages.get_messages(request):
            return render(request, 'admin.subcategories.create.html',
                          context={'current_user': request.user.name,
                                   'categories': categories})
        subcat = DatasetSubcategory()
        subcat.name = request.POST['name']
        subcat.categoryId = DatasetCategory.objects.get(pk=int(request.POST['categoryId']))
        subcat.save()
        messages.success(request, 'Subcategory created!')
        return HttpResponseRedirect(reverse('listcategories'))


def listusers(request: HttpRequest):
    check_invitation_statuses()
    users = User.objects.all().order_by('created_at')
    userlist = []

    for each in users:
        userlist.append({'id': each.pk, 'name': each.name, 'fullname': each.get_full_name(), 'created_at': each.created_at,
                         'status': 'active' if each.is_active else 'inactive'})

    if '.json' in urlparse(request.get_full_path()).path:
        return JsonResponse(userlist, safe=False)
    else:
        return render(request, 'admin.users.html', context={'current_user': request.user.name,
                                                            'users': userlist
                                                            })


def edituser(request: HttpRequest, userid: str):
    try:
        user = User.objects.get(pk=int(userid))
    except User.DoesNotExist:
        return HttpResponseNotFound('User does not exist!')
    if request.user.pk != int(userid):
        if not request.user.is_superuser:
            return HttpResponse('Permission denied!')
        else:
            userdict = {
                'id': user.pk,
                'name': user.name,
                'full_name': user.full_name if user.full_name else '',
                'active': user.is_active,
                'super': user.is_superuser
            }
            return render(request, 'admin.users.edit.html', context={'current_user': request.user.name,
                                                                     'user': userdict})
    else:
        userdict = {
            'id': user.pk,
            'name': user.name,
            'full_name': user.full_name if user.full_name else '',
            'selfedit': True
        }
        return render(request, 'admin.users.edit.html', context={'current_user': request.user.name,
                                                                 'user': userdict})


def manageuser(request: HttpRequest, userid: str):
    if request.user.pk != int(userid):
        if not request.user.is_superuser:
            return HttpResponse('Permission denied!')
    try:
        user = User.objects.get(pk=int(userid))
    except User.DoesNotExist:
        return HttpResponseNotFound('User does not exist!')

    if request.method == 'POST':
        request_dict = QueryDict(request.body.decode('utf-8')).dict()
        if request_dict['_method'] == 'DELETE':
            if request.user.pk != int(userid):
                try:
                    user.delete()
                except Exception as e:
                    messages.error(request, e.args[1])
                    return HttpResponseRedirect(reverse('listusers'))
                messages.success(request, 'User deleted.')
                return HttpResponseRedirect(reverse('listusers'))
            else:
                messages.error(request, 'You cannot delete yourself!')
                return HttpResponseRedirect(reverse('listusers'))
        if request_dict['_method'] == 'PATCH':
            request_dict.pop('_method', None)
            request_dict.pop('csrfmiddlewaretoken', None)
            full_name = request_dict['full_name'] if request_dict['full_name'] else None

            user.full_name = full_name

            if request.user.pk != int(userid):  # user cannot change 'active' or 'superuser' fields for himself
                is_active = True if request_dict.get('useractive', 0) else False
                is_superuser = True if request_dict.get('usersuper', 0) else False
                user.is_active = is_active
                user.is_superuser = is_superuser

            user.save()

            messages.success(request, 'User updated!')
            return HttpResponseRedirect(reverse('listusers'))

    if request.method == 'GET':
        return HttpResponseRedirect(reverse('listusers'))


def invite_user(request: HttpRequest):
    if request.method == 'GET':
        if not request.user.is_superuser:
            return HttpResponse('Permission denied!')
        else:
            form = InviteUserForm()
            return render(request, 'admin.invite_user.html', context={'form': form,
                                                                      'current_user': request.user.name})
    if request.method == 'POST':
        if not request.user.is_superuser:
            return HttpResponse('Permission denied!')
        else:
            form = InviteUserForm(request.POST)
            if form.is_valid():
                email = form.cleaned_data['email']
                name = form.cleaned_data['name']
                try:
                    newuser = User.objects.get(email=email)
                    messages.error(request, 'The user you are inviting is registered in the system.')
                    return render(request, 'admin.invite_user.html', context={'form': form,
                                                                              'current_user': request.user.name})
                except User.DoesNotExist:
                    pass
                try:
                    newuser = User.objects.get(name=name)
                    messages.error(request, 'The user with that name is registered in the system.')
                    return render(request, 'admin.invite_user.html', context={'form': form,
                                                                              'current_user': request.user.name})
                except User.DoesNotExist:
                    pass
                newuser = User()
                newuser.email = email
                newuser.name = name
                newuser.is_active = False
                newuser.is_superuser = False
                newuser.save()
                invitation = UserInvitation()
                invitation.code = get_random_string(length=40)
                invitation.email = newuser.email
                invitation.user_id = newuser
                invitation.status = 'pending'
                invitation.valid_till = timezone.now() + datetime.timedelta(days=2)
                invitation.save()
                newuser.email_user('Invitation to join OWID',
                                   'Hi %s, please follow this link in order '
                                   'to register on owid-grapher: %s' %
                                   (newuser.name, request.build_absolute_uri(reverse('registerbyinvite', args=[invitation.code]))),
                                   'no-reply@ourworldindata.org')
                messages.success(request, 'The invite was sent successfully.')
                return render(request, 'admin.invite_user.html', context={'form': InviteUserForm(),
                                                                          'current_user': request.user.name, })
            else:
                return render(request, 'admin.invite_user.html', context={'form': form,
                                                                          'current_user': request.user.name, })


def register_by_invite(request: HttpRequest, code: str):
    check_invitation_statuses()
    try:
        invite = UserInvitation.objects.get(code=code)
    except UserInvitation.DoesNotExist:
        return HttpResponseNotFound('Your invitation code does not exist in the system.')
    invited_user = invite.user_id

    if request.method == 'GET':
        if invite.status == 'successful':
            return HttpResponse('Your invitation code has already been used.')
        if invite.status == 'expired':
            return HttpResponse('Your invitation code has expired.')
        if invite.status == 'cancelled':
            return HttpResponse('Your invitation code has been cancelled.')
        form = InvitedUserRegisterForm(data={'name': invited_user.name})
        return render(request, 'register_invited_user.html', context={'form': form})
    if request.method == 'POST':
        form = InvitedUserRegisterForm(request.POST)
        if form.is_valid():
            name = form.cleaned_data['name']
            try:
                newuser = User.objects.get(name=name)
                if newuser != invited_user:
                    messages.error(request, 'The username you chose is not available. Please choose another username.')
                    return render(request, 'register_invited_user.html', context={'form': form})
            except User.DoesNotExist:
                pass
            if form.cleaned_data['password1'] == form.cleaned_data['password2']:
                newuser.name = name
                newuser.set_password(form.cleaned_data['password1'])
                newuser.is_active = True
                newuser.save()
                invite.status = 'successful'
                invite.save()
                return HttpResponseRedirect(reverse("login"))
            else:
                messages.error(request, "Passwords don't match!")
                return render(request, 'register_invited_user.html', context={'form': form})
        else:
            return render(request, 'register_invited_user.html', context={'form': form})


def write_dataset_csv(datasetid: int, new_dataset_name, old_dataset_name, committer, committer_email):

    """
    The function to write a dataset's csv file to a git repo
    :param datasetid: ID of a dataset to export
    :param new_dataset_name: The name of the file that will be written to disk
    :param old_dataset_name: If the dataset is being updated, we need its old name
    :param committer: Committer's name will show up in repo's commit info
    :param committer_email: Committer's email
    :return:
    """

    try:
        dataset = Dataset.objects.get(pk=datasetid)
    except Dataset.DoesNotExist:
        return

    # This location (base_repo_folder) shouldn't be a repo
    # All repos will be created automatically by the script on first export
    base_repo_folder = settings.DATASETS_REPO_LOCATION
    current_dataset_folder = os.path.abspath(os.path.join(base_repo_folder, dataset.namespace))
    temp_dataset_folder = os.path.abspath(settings.DATASETS_TMP_LOCATION)
    if not os.path.exists(base_repo_folder):
        os.makedirs(base_repo_folder)

    if not os.path.exists(current_dataset_folder):
        os.makedirs(current_dataset_folder)
        subprocess.check_output(
            'git init && '
            'git config user.name "%s" && '
            'git config user.email "%s"' %
            (settings.DATASETS_REPO_USERNAME, settings.DATASETS_REPO_EMAIL), shell=True,
            cwd=current_dataset_folder)
    else:
        if not os.path.exists(os.path.join(current_dataset_folder, '.git')):
            subprocess.check_output(
                'git init && '
                'git config user.name "%s" && '
                'git config user.email "%s"' %
                (settings.DATASETS_REPO_USERNAME, settings.DATASETS_REPO_EMAIL), shell=True,
                cwd=current_dataset_folder)

    allvariables = Variable.objects.filter(datasetId=dataset)

    if not allvariables:
        # if the dataset does not contain any data, no need to export it, just stop the script here
        return

    datasetvarlist = []
    vardata = []
    source_ids = []
    for var in allvariables.values('id', 'name', 'unit', 'description', 'code', 'coverage', 'timespan', 'sourceId__name', 'sourceId__id'):
        datasetvarlist.append({'id': var['id'], 'name': var['name']})
        vardata.append({
            'name': var['name'], 'unit': var['unit'], 'description': var['description'],
            'code': var['code'], 'coverage': var['coverage'], 'timespan': var['timespan'],
            'source_id': var['sourceId__name']
        })
        source_ids.append(var['sourceId__id'])

    datasetvarlist = sorted(datasetvarlist, key=lambda k: k['id'])

    id_tuple = ''
    varlist = []
    headerlist = ['Entity', 'Year']

    for each in datasetvarlist:
        id_tuple += str(each['id']) + ','
        headerlist.append(each['name'])
        varlist.append(each['id'])
    # removing that last comma
    id_tuple = id_tuple[:-1]
    # get all entity ids that have data values in the current dataset
    with connection.cursor() as entity_cursor:
        entity_cursor.execute('select distinct entityId from data_values where variableId in (%s);' %
                              ','.join([str(item['id']) for item in allvariables.values('id')]))
        dataset_entities_list = [item[0] for item in entity_cursor.fetchall()]
    # get the names for entity ids
    allentities = Entity.objects.filter(pk__in=dataset_entities_list).values('id', 'name')
    allentities_dict = {item['id']: item['name'] for item in allentities}
    allentities_list = []
    for each in allentities:
        allentities_list.append({'id': each['id'], 'name': each['name']})
    allentities_list = sorted(allentities_list, key=lambda k: k['name'])
    allentities_list = [item['id'] for item in allentities_list]
    # we are splitting the entities to groups of 3 for querying
    # this is still not very efficient, but better than querying values for each entity separately
    # if we don't set this limitation and query for values from all entities, th db usually hangs -
    # - when there are many rows present
    entity_chunks = [allentities_list[x:x + 3] for x in range(0, len(allentities_list), 3)]

    dataset_meta = {}

    with connection.cursor() as source_cursor:
        source_cursor.execute('select name, description from sources where id in (%s);' % ','.join([str(x) for x in source_ids]))
        dataset_meta['sources'] = [{'name': item[0], 'description': item[1]} for item in source_cursor.fetchall()]

    counter = 0
    source_name_to_id = {}
    for each in dataset_meta['sources']:
        each['id'] = counter
        source_name_to_id[each['name']] = counter
        counter += 1

    for each in vardata:
        each['source_id'] = source_name_to_id[each['source_id']]

    dataset_meta['variables'] = vardata

    sql_query = 'SELECT `value`, `year`, data_values.`variableId` as var_id, data_values.`entityId` as entity_id ' \
                ' from data_values ' \
                ' WHERE data_values.`variableId` in (%s) AND ' \
                'data_values.`entityId` in (%s) ORDER BY entityId, year, variableId;'

    metadata_filename = (unidecode(new_dataset_name) + '.json').replace('/', '_')
    dataset_filename = (unidecode(new_dataset_name) + '.csv').replace('/', '_')

    if old_dataset_name:
        old_metadata_name = (unidecode(old_dataset_name) + '.json').replace('/', '_')
        old_dataset_name = (unidecode(old_dataset_name) + '.csv').replace('/', '_')
        old_metadata_file_path = os.path.join(current_dataset_folder, old_metadata_name)
        old_dataset_file_path = os.path.join(current_dataset_folder, old_dataset_name)
        old_metadata_file_path_escaped = shlex.quote(os.path.join(current_dataset_folder, old_metadata_name))
        old_dataset_file_path_escaped = shlex.quote(os.path.join(current_dataset_folder, old_dataset_name))

    metadata_file_path_escaped = shlex.quote(os.path.join(current_dataset_folder, metadata_filename))
    dataset_file_path_escaped = shlex.quote(os.path.join(current_dataset_folder, dataset_filename))

    delete_previous = False
    if old_dataset_name is not None:
        if os.path.isfile(old_dataset_file_path):
            delete_previous = True
            commit_message = 'Updating: %s'
        else:
            commit_message = 'Creating: %s'
    else:
        commit_message = 'Creating: %s'

    # now saving the dataset to tmp dir

    with open(os.path.join(temp_dataset_folder, dataset_filename), 'w', newline='', encoding='utf8') as f:
        writer = csv.writer(f)
        writer.writerow(headerlist)
        current_row = None

        for chunk in entity_chunks:
            with connection.cursor() as outer_cursor:
                outer_cursor.execute(sql_query % (id_tuple, ','.join([str(x) for x in chunk])))
                rows = outer_cursor.fetchall()
            for row in rows:
                if not current_row or current_row[0] != row[3] or current_row[1] != row[1]:
                    if current_row:
                        current_row[0] = allentities_dict[current_row[0]]
                        writer.writerow(current_row)

                    current_row = [row[3], row[1]]
                    for i in range(0, len(varlist)):
                        current_row.append("")

                theindex = 2 + varlist.index(row[2])
                current_row[theindex] = row[0]

        current_row[0] = allentities_dict[current_row[0]]
        writer.writerow(current_row)

    with open(os.path.join(temp_dataset_folder, metadata_filename), 'w', encoding='utf8') as f:
        json.dump(dataset_meta, f, indent=4)

    try:
        if delete_previous:
            commit_hash = subprocess.check_output('git rm %s %s --quiet && '
                                                  'mv %s %s && mv %s %s && '
                                                  'git add %s %s && '
                                                  'git commit -m %s %s %s %s %s --quiet --author="%s <%s>" && '
                                                  'git rev-parse HEAD' %
                                                  (old_dataset_file_path_escaped,
                                                   old_metadata_file_path_escaped,
                                                   shlex.quote(os.path.join(temp_dataset_folder, dataset_filename)),
                                                   dataset_file_path_escaped,
                                                   shlex.quote(os.path.join(temp_dataset_folder, metadata_filename)),
                                                   metadata_file_path_escaped,
                                                   dataset_file_path_escaped,
                                                   metadata_file_path_escaped,
                                                   shlex.quote(commit_message % unidecode(dataset.name)),
                                                   dataset_file_path_escaped,
                                                   metadata_file_path_escaped,
                                                   old_metadata_file_path_escaped,
                                                   old_dataset_file_path_escaped,
                                                   committer, committer_email,
                                                   ), shell=True, cwd=current_dataset_folder)
        else:
            commit_hash = subprocess.check_output('mv %s %s && mv %s %s && '
                                                  'git add %s %s && '
                                                  'git commit -m %s %s %s --quiet --author="%s <%s>" && '
                                                  'git rev-parse HEAD' %
                                                  (shlex.quote(os.path.join(temp_dataset_folder, dataset_filename)),
                                                   dataset_file_path_escaped,
                                                   shlex.quote(os.path.join(temp_dataset_folder, metadata_filename)),
                                                   metadata_file_path_escaped,
                                                   dataset_file_path_escaped,
                                                   metadata_file_path_escaped,
                                                   shlex.quote(commit_message % unidecode(dataset.name)),
                                                   metadata_file_path_escaped,
                                                   dataset_file_path_escaped,
                                                   committer, committer_email,
                                                   ), shell=True, cwd=current_dataset_folder)
    except subprocess.CalledProcessError as e:
        if 'nothing to commit' in e.output.decode('utf-8').lower():
            return
        else:
            raise Exception('An error occured while exporting the dataset to the git repo.')

    commit_hash = commit_hash.decode('utf-8').strip()
    # now saving the diff show output to html
    conv = Ansi2HTMLConverter()
    commit_info = subprocess.check_output('git -C %s show --color %s'
                                          % (current_dataset_folder, commit_hash), shell=True)
    html = conv.convert(commit_info.decode('utf-8'))
    if not os.path.exists(os.path.join(settings.DATASETS_DIFF_HTML_LOCATION, dataset.namespace)):
        os.makedirs(os.path.join(settings.DATASETS_DIFF_HTML_LOCATION, dataset.namespace))
    with open(os.path.join(settings.DATASETS_DIFF_HTML_LOCATION, dataset.namespace, '%s.html' % commit_hash),
              'w', encoding='utf8') as f:
        f.write(html)


def show_dataset_history(request: HttpRequest, datasetid: str):
    try:
        dataset = Dataset.objects.get(pk=int(datasetid))
    except Dataset.DoesNotExist:
        return HttpResponseNotFound('Dataset does not exist!')

    repo_folder = os.path.join(settings.DATASETS_REPO_LOCATION, dataset.namespace)
    history = None
    meta_history = None
    commit_fields = ['commit_hash', 'commit_made_by', 'commit_date']
    log_format = ['%H', '%an', '%ad']
    # %x1f and %x1e are for delimiting and separating each record
    log_format = '%x1f'.join(log_format) + '%x1e'
    if os.path.exists(repo_folder):
        try:
            history = subprocess.check_output('git log --format="%s" --follow %s ' %
                                              (log_format, shlex.quote(dataset.name + '.csv')), shell=True,
                                              cwd=repo_folder)
        except subprocess.CalledProcessError:
            # subprocess will raise exception if git doesn't find any commits related to the given file name
            pass
        try:
            meta_history = subprocess.check_output('git log --format="%s" --follow %s ' %
                                                   (log_format, shlex.quote(dataset.name + '.json')), shell=True,
                                                   cwd=repo_folder)
        except subprocess.CalledProcessError:
            pass

    if history:
        history = history.decode('utf-8').strip('\n\x1e').split("\x1e")
        history = [row.strip().split("\x1f") for row in history]
        history = [dict(zip(commit_fields, row)) for row in history]
        # we will need to parse the date string returned by git
        for each in history:
            each['commit_date'] = parser.parse(each['commit_date'])
            each['namespace'] = dataset.namespace
    if meta_history:
        meta_history = meta_history.decode('utf-8').strip('\n\x1e').split("\x1e")
        meta_history = [row.strip().split("\x1f") for row in meta_history]
        meta_history = [dict(zip(commit_fields, row)) for row in meta_history]
        # we will need to parse the date string returned by git
        for each in meta_history:
            each['commit_date'] = parser.parse(each['commit_date'])
            each['namespace'] = dataset.namespace

    return render(request, 'admin.datasets.history.html', context={'current_user': request.user.name,
                                                                   'dataset_name': dataset.name,
                                                                   'history': history,
                                                                   'meta_history': meta_history,
                                                                   'datasetid': dataset.id
                                                                   })


def serve_diff_html(request: HttpRequest, namespace: str, commit_hash: str):
    if os.path.isfile(os.path.join(settings.DATASETS_DIFF_HTML_LOCATION, namespace, '%s.html' % commit_hash)):
        return HttpResponse(open(os.path.join(settings.DATASETS_DIFF_HTML_LOCATION, namespace, '%s.html' % commit_hash),
                                 'r', encoding='utf8').read())
    else:
        return HttpResponse('No diff file found for that commit!')


def all_dataset_history(request: HttpRequest):

    items_per_page = 50

    namespaces = Dataset.objects.all().values('namespace').distinct()

    history = []

    commit_fields = ['commit_hash', 'commit_made_by', 'commit_date', 'message']
    log_format = ['%H', '%an', '%ad', '%s']
    # %x1f and %x1e are for delimiting and separating each record
    log_format = '%x1f'.join(log_format) + '%x1e'

    for namespace in namespaces:
        if os.path.exists(os.path.join(settings.DATASETS_REPO_LOCATION, namespace['namespace'])):
            try:
                output = subprocess.check_output('git log --format="%s"' %
                                                 log_format, shell=True,
                                                 cwd=os.path.join(settings.DATASETS_REPO_LOCATION, namespace['namespace']))
            except subprocess.CalledProcessError:
                # subprocess will raise exception if git doesn't find any commits
                # we then give back a None
                output = None

            if output:
                output = output.decode('utf-8').strip('\n\x1e').split("\x1e")
                output = [row.strip().split("\x1f") for row in output]
                output = [dict(zip(commit_fields, row)) for row in output]
                # we will need to parse the date string returned by git
                for each in output:
                    each['commit_date'] = parser.parse(each['commit_date'])
                    each['namespace'] = namespace['namespace']
                    history.append(each)

    # sort everything by date
    history = sorted(history, key=lambda k: k['commit_date'], reverse=True)

    total_rows = len(history)
    total_pages = -(-len(history) // items_per_page)
    page_number = get_query_as_dict(request).get('page', [0])

    try:
        page_number = int(page_number[0])
    except ValueError:
        page_number = 0

    if page_number > 1:
        vals = history[(page_number - 1) * items_per_page:page_number * items_per_page]
    else:
        vals = history[:items_per_page]

    if vals:
        if total_pages >= 13:
            if page_number < 7:
                nav_pages = [1, 2, 3, 4, 5, 6, 7, 8, '#', total_pages - 1, total_pages]
            elif page_number > total_pages - 5:
                nav_pages = [1, 2, '#', total_pages - 7, total_pages - 6, total_pages - 5, total_pages - 4,
                             total_pages - 3,
                             total_pages - 2, total_pages - 1, total_pages]
            else:
                nav_pages = [1, 2, '#', page_number - 3, page_number - 2, page_number - 1, page_number,
                             page_number + 1,
                             page_number + 2, page_number + 3, '#', total_pages - 1, total_pages]
        else:
            nav_pages = [n for n in range(1, total_pages + 1)]
    else:
        nav_pages = []

    return render(request, 'admin.datasets.all_history.html', context={'current_user': request.user.name,
                                                                       'history_items': vals,
                                                                       'nav_pages': nav_pages,
                                                                       'current_page': page_number,
                                                                       'total_rows': total_rows
                                                                       })


def serve_commit_file(request: HttpRequest, namespace: str, commit_hash: str, filetype: str):

    repo_folder = os.path.join(settings.DATASETS_REPO_LOCATION, namespace)

    files_list = subprocess.check_output(
        'git diff-tree --no-commit-id --name-status  -r %s' % commit_hash,
        shell=True,
        cwd=repo_folder
    )

    if not files_list:  # the commit might be the very first commit
        files_list = subprocess.check_output(
            'git diff-tree --no-commit-id --name-status  --root %s' % commit_hash,
            shell=True,
            cwd=repo_folder
        )

    files_list = files_list.decode('utf-8').splitlines()

    file_type_dict = {}
    for each in files_list:
        status = each.split('\t')[0]
        filename = each.split('\t')[1]
        if status != 'D':
            file_type_dict[os.path.splitext(filename)[1]] = filename

    filetype = '.%s' % filetype
    if filetype in file_type_dict:
        file_contents = subprocess.check_output('git show %s:%s' % (commit_hash, shlex.quote(file_type_dict[filetype])),
                                                shell=True, cwd=repo_folder)
        file_contents = file_contents.decode('utf-8')
        response = HttpResponse(file_contents)
        ascii_filename = unidecode(file_type_dict[filetype])
        disposition = "attachment; filename='%s'" % ascii_filename
        response['Content-Disposition'] = disposition
        return response
    else:
        return HttpResponse('Could not get file contents.')


def treeview_datasets(request: HttpRequest):
    tree = []
    tree_dict = {}
    all_variables = Variable.objects.all().values('id', 'name', 'datasetId__categoryId__name',
                                                  'datasetId__subcategoryId__name', 'datasetId__name', 'datasetId').iterator()

    for var in all_variables:
        if var['datasetId__categoryId__name'] not in tree_dict:
            tree_dict[var['datasetId__categoryId__name']] = {}

        if var['datasetId__subcategoryId__name'] not in tree_dict[var['datasetId__categoryId__name']]:
            tree_dict[var['datasetId__categoryId__name']][var['datasetId__subcategoryId__name']] = {}

        if var['datasetId__name'] not in tree_dict[var['datasetId__categoryId__name']][
            var['datasetId__subcategoryId__name']]:
            tree_dict[var['datasetId__categoryId__name']][var['datasetId__subcategoryId__name']][
                var['datasetId__name']] = {'id': var['datasetId'], 'vars': []}

        tree_dict[var['datasetId__categoryId__name']][var['datasetId__subcategoryId__name']][
            var['datasetId__name']]['vars'].append({'varname': var['name'], 'varid': var['id']})
    cat_id_count = 0
    subcat_id_count = 0
    for cat, catcontent in tree_dict.items():
        subcatlist = []
        subcatcount = 0
        cat_id_count += 1
        for subcat, subcatcontent in catcontent.items():
            datasetlist = []
            datasetcount = 0
            subcat_id_count += 1
            for dataset, datasetcontent in subcatcontent.items():
                varlist = []
                varcount = 0
                for onevar in datasetcontent['vars']:
                    varlist.append({'text': onevar['varname'], 'selectable': False, 'backColor': "#FF5C5C",
                                    'href': reverse('showvariable', args=(onevar['varid'], ))})
                    varcount += 1
                datasetlist.append({'text': dataset + ' - (%s)' % str(varcount), 'selectable': False,
                                    'backColor': "#5697FF",
                                    'href': reverse('showdataset', args=(datasetcontent['id'], )), 'nodes': varlist})
                datasetcount += 1
            subcatlist.append({'text': subcat + ' - (%s)' % str(datasetcount),
                               'selectable': False, 'backColor': "#AAFF5C", 'href': "#subcat%s" % subcat_id_count,
                               'nodes': datasetlist})
            subcatcount += 1
        tree.append({'text': cat + ' - (%s)' % str(subcatcount), 'selectable': False, 'href': "#cat%s" % cat_id_count,
                     'nodes': subcatlist})

    for cat in tree:
        cat['nodes'] = sorted(cat['nodes'], key=lambda k: k['text'])
        for subcat in cat['nodes']:
            subcat['nodes'] = sorted(subcat['nodes'], key=lambda k: k['text'])
            for dataset in subcat['nodes']:
                dataset['nodes'] = sorted(dataset['nodes'], key=lambda k: k['text'])

    tree = sorted(tree, key=lambda k: k['text'])

    tree_json = json.dumps(tree)

    return render(request, 'admin.datasets.by.category.html', context={'current_user': request.user.name,
                                                                       'tree_json': tree_json
                                                                       })

def buildstatus(request):
    r = requests.get(f"https://api.netlify.com/api/v1/sites/a7b9d6fc-5c50-41b8-b37f-9387e90c356d/deploys?access_token={settings.NETLIFY_ACCESS_TOKEN}")
    return JsonResponse(r.json()[0])


def variables(request, ids):
    """
    :param request: Request object
    :param ids: ids of requested variables
    :return: json file of requested variables in plain text format
    """
    meta = { "variables": {} }

    # First, grab all the variable metadata needed by the frontend
    variable_ids = [int(idStr) for idStr in ids.split('+')]
    variables = Variable.objects.filter(id__in=variable_ids).select_related('datasetId', 'sourceId').values(
        'id', 'name', 'description', 'unit', 'short_unit',
        'display', 'datasetId__name', 'sourceId__pk', 'sourceId__name', 'sourceId__description'
    )

    # Process the metadata into a nicer form
    for variable in variables:
        shortUnit = variable.pop('short_unit')
        if shortUnit is not None:
            variable['shortUnit'] = shortUnit
        variable['datasetName'] = variable.pop('datasetId__name')
        source_description = json.loads(variable.pop('sourceId__description'))
        variable['source'] = source_description
        variable['source']['id'] = variable.pop('sourceId__pk')
        variable['source']['name'] = variable.pop('sourceId__name')
        variable['source']['dataPublishedBy'] = "" if not source_description['dataPublishedBy'] else source_description['dataPublishedBy']
        variable['source']['dataPublisherSource'] = "" if not source_description['dataPublisherSource'] else source_description[
            'dataPublisherSource']
        variable['source']['link'] = "" if not source_description['link'] else source_description['link']
        variable['source']['retrievedDate'] = "" if not source_description['retrievedDate'] else source_description['retrievedDate']
        variable['source']['additionalInfo'] = "" if not source_description['additionalInfo'] else source_description[
            'additionalInfo']
        meta['variables'][variable['id']] = variable

    # Now fetch the actual data, using a custom csv-like transfer format
    # for efficiency (this is the most common expensive operation in the grapher)
    varstring = ""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT value, year, variableId as var_id, entities.id as entity_id, 
            entities.name as entity_name, entities.code as entity_code 
            FROM data_values 
            LEFT JOIN entities ON data_values.entityId = entities.id 
            WHERE data_values.variableId IN %s 
            ORDER BY var_id ASC, year ASC
        """, [variable_ids])
        rows = dictfetchall(cursor)

    def stream():
        yield json.dumps(meta)

        entitykey = {}
        seen_variables = {}
        for row in rows:
            if row['var_id'] not in seen_variables:
                seen_variables[row['var_id']] = True
                yield "\r\n"
                yield str(row['var_id'])

            yield f";{row['year']},{row['entity_id']},{row['value']}"

            if row['entity_id'] not in entitykey:
                entitykey[row['entity_id']] = {'name': row['entity_name'], 'code': row['entity_code']}

        yield "\r\n"
        yield json.dumps(entitykey)

    response = StreamingHttpResponse(stream(), content_type="text/plain")

    if get_query_string(request):
        response['Cache-Control'] = 'max-age=31536000 public'
    else:
        response['Cache-Control'] = 'no-cache'
    response['Access-Control-Allow-Origin'] = '*'

    return response
