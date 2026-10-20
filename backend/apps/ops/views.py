import ast
import base64
import json
import time
import re
from django.shortcuts import render
from common.utils.Tengcentcos_utils import put_object, get_presigned_url
from apps.company.models import Company
from apps.opengate.models import Gates
from apps.ops.models import CompanyFollow, GateFollow, Partner, PopularCompany, PopularGate
from apps.user.models import User
from common.utils.Login_utils import login_checker, get_client_ip
from apps.ops.services import OpsService
from django.http import JsonResponse

# The rest of ops views should be migrated here; omitted for brevity in this edit context.

# Create your views here.

@login_checker
def list_hot_gate(request):
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id)
    except Exception:
        return JsonResponse({'result': 0, 'message': 'user not found'}, status=404)
    if user.role == 'user':
        return JsonResponse({'result': 0, 'message': 'user is not admin'}, status=403)
    data = OpsService.list_hot_gate(client_ip=get_client_ip(request))
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})

@login_checker
def list_hot_company(request):
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id)
    except Exception:
        return JsonResponse({'result': 0, 'message': 'user not found'}, status=404)
    if user.role == 'user':
        return JsonResponse({'result': 0, 'message': 'user is not admin'}, status=403)
    data = OpsService.list_hot_company(client_ip=get_client_ip(request))
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})


def get_popular_gate(request):
    data = OpsService.get_popular_gate()
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})

def get_popular_company(request):
    data = OpsService.get_popular_company()
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})

def get_partner(request):
    data = OpsService.get_partners()
    return JsonResponse({'result': 1, 'message': 'success', 'data': data})


@login_checker
def set_popular_gate(request):
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id)
    except Exception as e:
        return JsonResponse({'result': 0, 'message': 'user not found'}, status=404)
    if user.role == 'user':
        return JsonResponse({'result': 0, 'message': 'user is not admin'}, status=403)
    
    ids = request.POST.getlist('gate_id') or []
    ids = [int(i) for i in ids if str(i).isdigit()]
    ok = OpsService.set_popular_gate(ids, client_ip=get_client_ip(request))
    return JsonResponse({'result': 1 if ok else 0, 'message': 'success' if ok else 'failed'})

@login_checker
def set_popular_company(request):
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id)
    except Exception as e:
        return JsonResponse({'result': 0, 'message': 'user not found'}, status=404)
    if user.role == 'user':
        return JsonResponse({'result': 0, 'message': 'user is not admin'}, status=403)
    
    ids = request.POST.getlist('company_id') or []
    ids = [int(i) for i in ids if str(i).isdigit()]
    ok = OpsService.set_popular_company(ids, client_ip=get_client_ip(request))
    return JsonResponse({'result': 1 if ok else 0, 'message': 'success' if ok else 'failed'})

@login_checker
def set_partner(request):
    user_id = request.user_id
    try:
        user = User.objects.get(id=user_id)
    except Exception as e:
        return JsonResponse({'result': 0, 'message': 'user not found'}, status=404)
    if user.role == 'user':
        return JsonResponse({'result': 0, 'message': 'user is not admin'}, status=403)
    
    data_json = json.loads(request.body.decode())
    logo = data_json.get('logo')
    name = data_json.get('name')
    description = data_json.get('description')
    website = data_json.get('website')
    ok = OpsService.create_partner(logo, name, description, website, client_ip=get_client_ip(request))
    return JsonResponse({'result': 1 if ok else 0, 'message': 'success' if ok else 'failed'})

