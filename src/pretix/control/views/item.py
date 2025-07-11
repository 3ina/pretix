#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020 Raphael Michel and contributors
# Copyright (C) 2020-2021 rami.io GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#

# This file is based on an earlier version of pretix which was released under the Apache License 2.0. The full text of
# the Apache License 2.0 can be obtained at <http://www.apache.org/licenses/LICENSE-2.0>.
#
# This file may have since been changed and any changes are released under the terms of AGPLv3 as described above. A
# full history of changes and contributors is available at <https://github.com/pretix/pretix>.
#
# This file contains Apache-licensed contributions copyrighted by: Adam K. Sumner, Enrique Saez, Jakob Schnell, Sohalt,
# Tobias Kunze, Ture Gjørup
#
# Unless required by applicable law or agreed to in writing, software distributed under the Apache License 2.0 is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under the License.

import json
from collections import OrderedDict, namedtuple
from itertools import groupby
from json.decoder import JSONDecodeError

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.files import File
from django.db import transaction
from django.db.models import (
    Count, Exists, F, OuterRef, Prefetch, ProtectedError, Q,
)
from django.forms.models import inlineformset_factory
from django.http import (
    Http404, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect,
)
from django.shortcuts import redirect
from django.urls import resolve, reverse
from django.utils.functional import cached_property
from django.utils.timezone import now
from django.utils.translation import gettext, gettext_lazy as _
from django.views.decorators.http import require_http_methods
from django.views.generic import ListView
from django.views.generic.detail import DetailView, SingleObjectMixin
from django_countries.fields import Country

from pretix.api.serializers.item import (
    ItemAddOnSerializer, ItemBundleSerializer, ItemVariationSerializer,
)
from pretix.base.forms import I18nFormSet
from pretix.base.models import (
    CartPosition, Item, ItemCategory, ItemVariation, Order, OrderPosition,
    Question, QuestionAnswer, QuestionOption, Quota, SeatCategoryMapping,
    Voucher,
)
from pretix.base.models.event import SubEvent
from pretix.base.models.items import ItemAddOn, ItemBundle, ItemMetaValue
from pretix.base.services.quotas import QuotaAvailability
from pretix.base.services.tickets import invalidate_cache
from pretix.base.signals import quota_availability
from pretix.control.forms.item import (
    CategoryForm, ItemAddOnForm, ItemAddOnsFormSet, ItemBundleForm,
    ItemBundleFormSet, ItemCreateForm, ItemMetaValueForm, ItemUpdateForm,
    ItemVariationForm, ItemVariationsFormSet, QuestionForm, QuestionOptionForm,
    QuotaForm,
)
from pretix.control.permissions import (
    EventPermissionRequiredMixin, event_permission_required,
)
from pretix.control.signals import item_forms, item_formsets
from pretix.helpers.models import modelcopy

from ...helpers.compat import CompatDeleteView
from . import ChartContainingView, CreateView, PaginationMixin, UpdateView


def has_truthy_attr(cls, attr):
    return hasattr(cls, attr) and getattr(cls, attr)


class ItemList(ListView):
    model = Item
    context_object_name = 'items'
    # paginate_by = 30
    # Pagination is disabled as it is very unlikely to be necessary
    # here and could cause problems with the "reorder-within-category" feature
    template_name = 'pretixcontrol/items/index.html'

    def get_queryset(self):
        requires_seat = Exists(
            SeatCategoryMapping.objects.filter(
                product_id=OuterRef('pk'),
            )
        )
        return Item.objects.filter(
            event=self.request.event
        ).select_related("tax_rule").annotate(
            var_count=Count('variations'),
            requires_seat=requires_seat,
        ).prefetch_related("category", "limit_sales_channels").order_by(
            F('category__position').asc(nulls_first=True),
            'category', 'position'
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['sales_channels'] = self.request.organizer.sales_channels.all()
        items_by_category = {cat: list(items) for cat, items in groupby(ctx['items'], lambda item: item.category)}
        ctx['cat_list'] = [(cat, items_by_category.get(cat, [])) for cat in [None, *self.request.event.categories.all()]]
        return ctx


def item_move(request, item, up=True):
    """
    This is a helper function to avoid duplicating code in item_move_up and
    item_move_down. It takes an item and a direction and then tries to bring
    all items for this category in a new order.
    """
    try:
        item = request.event.items.get(
            id=item
        )
    except Item.DoesNotExist:
        raise Http404(_("The requested product does not exist."))
    items = list(request.event.items.filter(category=item.category).order_by("position"))

    index = items.index(item)
    if index != 0 and up:
        items[index - 1], items[index] = items[index], items[index - 1]
    elif index != len(items) - 1 and not up:
        items[index + 1], items[index] = items[index], items[index + 1]

    for i, item in enumerate(items):
        if item.position != i:
            item.position = i
            item.save()
            item.log_action(
                'pretix.event.item.reordered', user=request.user, data={
                    'position': i,
                }
            )
    messages.success(request, _('The order of items has been updated.'))


@event_permission_required("can_change_items")
@require_http_methods(["POST"])
def item_move_up(request, organizer, event, item):
    item_move(request, item, up=True)
    return redirect('control:event.items',
                    organizer=request.event.organizer.slug,
                    event=request.event.slug)


@event_permission_required("can_change_items")
@require_http_methods(["POST"])
def item_move_down(request, organizer, event, item):
    item_move(request, item, up=False)
    return redirect('control:event.items',
                    organizer=request.event.organizer.slug,
                    event=request.event.slug)


@transaction.atomic
@event_permission_required("can_change_items")
@require_http_methods(["POST"])
def reorder_items(request, organizer, event, category):
    try:
        ids = json.loads(request.body.decode('utf-8'))['ids']
    except (JSONDecodeError, KeyError, ValueError):
        return HttpResponseBadRequest("expected JSON: {ids:[]}")

    input_items = list(request.event.items.filter(id__in=[i for i in ids if i.isdigit()]))

    if len(input_items) != len(ids):
        raise Http404(_("Some of the provided object ids are invalid."))

    if int(category):
        target_category = request.event.categories.get(id=category)
    else:
        target_category = None

    for i in input_items:
        pos = ids.index(str(i.pk))
        if pos != i.position or target_category != i.category:  # Save unneccessary UPDATE queries
            i.position = pos
            i.category = target_category
            i.save(update_fields=['position', 'category_id'])
            i.log_action(
                'pretix.event.item.reordered', user=request.user, data={
                    'position': i,
                    'category': target_category and target_category.pk,
                }
            )

    return HttpResponse()


class CategoryDelete(EventPermissionRequiredMixin, CompatDeleteView):
    model = ItemCategory
    template_name = 'pretixcontrol/items/category_delete.html'
    permission = 'can_change_items'
    context_object_name = 'category'

    def get_object(self, queryset=None) -> ItemCategory:
        try:
            return self.request.event.categories.get(
                id=self.kwargs['category']
            )
        except ItemCategory.DoesNotExist:
            raise Http404(_("The requested product category does not exist."))

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        for item in self.object.items.all():
            item.category = None
            item.save()
        success_url = self.get_success_url()
        self.object.log_action('pretix.event.category.deleted', user=self.request.user)
        self.object.delete()
        messages.success(request, _('The selected category has been deleted.'))
        return HttpResponseRedirect(success_url)

    def get_success_url(self) -> str:
        return reverse('control:event.items.categories', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })


class CategoryUpdate(EventPermissionRequiredMixin, UpdateView):
    model = ItemCategory
    form_class = CategoryForm
    template_name = 'pretixcontrol/items/category.html'
    permission = 'can_change_items'
    context_object_name = 'category'

    def get_object(self, queryset=None) -> ItemCategory:
        url = resolve(self.request.path_info)
        try:
            return self.request.event.categories.get(
                id=url.kwargs['category']
            )
        except ItemCategory.DoesNotExist:
            raise Http404(_("The requested product category does not exist."))

    @transaction.atomic
    def form_valid(self, form):
        messages.success(self.request, _('Your changes have been saved.'))
        if form.has_changed():
            self.object.log_action(
                'pretix.event.category.changed', user=self.request.user, data={
                    k: form.cleaned_data.get(k) for k in form.changed_data
                }
            )
        return super().form_valid(form)

    def get_success_url(self) -> str:
        return reverse('control:event.items.categories', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)


class CategoryCreate(EventPermissionRequiredMixin, CreateView):
    model = ItemCategory
    form_class = CategoryForm
    template_name = 'pretixcontrol/items/category.html'
    permission = 'can_change_items'
    context_object_name = 'category'

    def get_success_url(self) -> str:
        return reverse('control:event.items.categories', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })

    @cached_property
    def copy_from(self):
        if self.request.GET.get("copy_from") and not getattr(self, 'object', None):
            try:
                return self.request.event.categories.get(pk=self.request.GET.get("copy_from"))
            except ItemCategory.DoesNotExist:
                pass

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()

        if self.copy_from:
            i = modelcopy(self.copy_from)
            i.pk = None
            kwargs['instance'] = i
            kwargs.setdefault('initial', {})
            kwargs['initial']['cross_selling_match_products'] = [str(i.pk) for i in self.copy_from.cross_selling_match_products.all()]
        else:
            kwargs['instance'] = ItemCategory(event=self.request.event)
        return kwargs

    @transaction.atomic
    def form_valid(self, form):
        form.instance.event = self.request.event
        messages.success(self.request, _('The new category has been created.'))
        ret = super().form_valid(form)
        form.instance.log_action('pretix.event.category.added', data=dict(form.cleaned_data), user=self.request.user)
        return ret

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)


class CategoryList(PaginationMixin, ListView):
    model = ItemCategory
    context_object_name = 'categories'
    template_name = 'pretixcontrol/items/categories.html'

    def get_queryset(self):
        return self.request.event.categories.all()


def category_move(request, category, up=True):
    """
    This is a helper function to avoid duplicating code in category_move_up and
    category_move_down. It takes a category and a direction and then tries to bring
    all categories for this event in a new order.
    """
    try:
        category = request.event.categories.get(
            id=category
        )
    except ItemCategory.DoesNotExist:
        raise Http404(_("The requested product category does not exist."))
    categories = list(request.event.categories.order_by("position"))

    index = categories.index(category)
    if index != 0 and up:
        categories[index - 1], categories[index] = categories[index], categories[index - 1]
    elif index != len(categories) - 1 and not up:
        categories[index + 1], categories[index] = categories[index], categories[index + 1]

    for i, cat in enumerate(categories):
        if cat.position != i:
            cat.position = i
            cat.save()
            cat.log_action(
                'pretix.event.category.reordered', user=request.user, data={
                    'position': i,
                }
            )
    messages.success(request, _('The order of categories has been updated.'))


@event_permission_required("can_change_items")
@require_http_methods(["POST"])
def category_move_up(request, organizer, event, category):
    category_move(request, category, up=True)
    return redirect('control:event.items.categories',
                    organizer=request.event.organizer.slug,
                    event=request.event.slug)


@event_permission_required("can_change_items")
@require_http_methods(["POST"])
def category_move_down(request, organizer, event, category):
    category_move(request, category, up=False)
    return redirect('control:event.items.categories',
                    organizer=request.event.organizer.slug,
                    event=request.event.slug)


@transaction.atomic
@event_permission_required("can_change_items")
@require_http_methods(["POST"])
def reorder_categories(request, organizer, event):
    try:
        ids = json.loads(request.body.decode('utf-8'))['ids']
    except (JSONDecodeError, KeyError, ValueError):
        return HttpResponseBadRequest("expected JSON: {ids:[]}")

    input_categories = list(request.event.categories.filter(id__in=[i for i in ids if i.isdigit()]))

    if len(input_categories) != len(ids):
        raise Http404(_("Some of the provided object ids are invalid."))

    if len(input_categories) != request.event.categories.count():
        raise Http404(_("Not all objects have been selected."))

    for c in input_categories:
        pos = ids.index(str(c.pk))
        if pos != c.position:  # Save unneccessary UPDATE queries
            c.position = pos
            c.save(update_fields=['position'])
            c.log_action(
                'pretix.event.category.reordered', user=request.user, data={
                    'position': pos,
                }
            )

    return HttpResponse()


FakeQuestion = namedtuple(
    'FakeQuestion', 'id question position required'
)


class QuestionList(ListView):
    model = Question
    context_object_name = 'questions'
    template_name = 'pretixcontrol/items/questions.html'

    def get_queryset(self):
        return self.request.event.questions.prefetch_related('items')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        questions = []

        if self.request.event.settings.attendee_names_asked:
            questions.append(
                FakeQuestion(
                    id='attendee_name_parts',
                    question=_('Attendee name'),
                    position=self.request.event.settings.system_question_order.get(
                        'attendee_name_parts', 0
                    ),
                    required=self.request.event.settings.attendee_names_required,
                )
            )

        if self.request.event.settings.attendee_emails_asked:
            questions.append(
                FakeQuestion(
                    id='attendee_email',
                    question=_('Attendee email'),
                    position=self.request.event.settings.system_question_order.get(
                        'attendee_email', 0
                    ),
                    required=self.request.event.settings.attendee_emails_required,
                )
            )

        if self.request.event.settings.attendee_company_asked:
            questions.append(
                FakeQuestion(
                    id='company',
                    question=_('Company'),
                    position=self.request.event.settings.system_question_order.get(
                        'company', 0
                    ),
                    required=self.request.event.settings.attendee_company_required,
                )
            )

        if self.request.event.settings.attendee_addresses_asked:
            questions.append(
                FakeQuestion(
                    id='street',
                    question=_('Street'),
                    position=self.request.event.settings.system_question_order.get(
                        'street', 0
                    ),
                    required=self.request.event.settings.attendee_addresses_required,
                )
            )
            questions.append(
                FakeQuestion(
                    id='zipcode',
                    question=_('ZIP code'),
                    position=self.request.event.settings.system_question_order.get(
                        'zipcode', 0
                    ),
                    required=self.request.event.settings.attendee_addresses_required,
                )
            )
            questions.append(
                FakeQuestion(
                    id='city',
                    question=_('City'),
                    position=self.request.event.settings.system_question_order.get(
                        'city', 0
                    ),
                    required=self.request.event.settings.attendee_addresses_required,
                )
            )
            questions.append(
                FakeQuestion(
                    id='country',
                    question=_('Country'),
                    position=self.request.event.settings.system_question_order.get(
                        'country', 0
                    ),
                    required=self.request.event.settings.attendee_addresses_required,
                )
            )

        questions += list(ctx['questions'])
        questions.sort(key=lambda q: q.position)
        ctx['questions'] = questions
        return ctx


@transaction.atomic
@event_permission_required("can_change_items")
@require_http_methods(["POST"])
def reorder_questions(request, organizer, event):
    try:
        ids = json.loads(request.body.decode('utf-8'))['ids']
    except (JSONDecodeError, KeyError, ValueError):
        return HttpResponseBadRequest("expected JSON: {ids:[]}")

    # filter system_questions - normal questions are int/digit, system_questions strings
    custom_question_ids = [i for i in ids if i.isdigit()]
    input_questions = list(request.event.questions.filter(id__in=custom_question_ids))

    if len(input_questions) != len(custom_question_ids):
        raise Http404(_("Some of the provided object ids are invalid."))

    if len(input_questions) != request.event.questions.count():
        raise Http404(_("Not all objects have been selected."))

    for q in input_questions:
        pos = ids.index(str(q.pk))
        if pos != q.position:  # Save unneccessary UPDATE queries
            q.position = pos
            q.save(update_fields=['position'])
            q.log_action(
                'pretix.event.question.reordered', user=request.user, data={
                    'position': pos,
                }
            )

    system_question_order = {}
    for s in ('attendee_name_parts', 'attendee_email', 'company', 'street', 'zipcode', 'city', 'country'):
        if s in ids:
            system_question_order[s] = ids.index(s)
        else:
            system_question_order[s] = -1
    request.event.settings.system_question_order = system_question_order
    request.event.log_action(
        'pretix.event.settings', user=request.user, data={
            'system_question_order': system_question_order,
        }
    )

    return HttpResponse()


class QuestionDelete(EventPermissionRequiredMixin, CompatDeleteView):
    model = Question
    template_name = 'pretixcontrol/items/question_delete.html'
    permission = 'can_change_items'
    context_object_name = 'question'

    def get_object(self, queryset=None) -> Question:
        try:
            return self.request.event.questions.get(
                id=self.kwargs['question']
            )
        except Question.DoesNotExist:
            raise Http404(_("The requested question does not exist."))

    def get_context_data(self, *args, **kwargs) -> dict:
        context = super().get_context_data(*args, **kwargs)
        context['dependent'] = list(self.get_object().items.all())
        context['edit_url'] = reverse('control:event.items.questions.edit', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
            'question': self.get_object().pk,
        })
        return context

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        success_url = self.get_success_url()
        self.object.log_action(action='pretix.event.question.deleted', user=request.user)
        self.object.delete()
        messages.success(request, _('The selected question has been deleted.'))
        return HttpResponseRedirect(success_url)

    def get_success_url(self) -> str:
        return reverse('control:event.items.questions', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })


class QuestionMixin:
    @cached_property
    def formset(self):
        formsetclass = inlineformset_factory(
            Question, QuestionOption,
            form=QuestionOptionForm, formset=I18nFormSet,
            can_order=True, can_delete=True, extra=0
        )
        return formsetclass(self.request.POST if self.request.method == "POST" else None,
                            queryset=(QuestionOption.objects.filter(question=self.object)
                                      if self.object else QuestionOption.objects.none()),
                            event=self.request.event)

    def save_formset(self, obj):
        if self.formset.is_valid():
            for form in self.formset.initial_forms:
                if form in self.formset.deleted_forms:
                    if not form.instance.pk:
                        continue
                    obj.log_action(
                        'pretix.event.question.option.deleted', user=self.request.user, data={
                            'id': form.instance.pk
                        }
                    )
                    form.instance.delete()
                    form.instance.pk = None

            forms = self.formset.ordered_forms + [
                ef for ef in self.formset.extra_forms
                if ef not in self.formset.ordered_forms and ef not in self.formset.deleted_forms
            ]
            for i, form in enumerate(forms):
                form.instance.position = i
                form.instance.question = obj
                created = not form.instance.pk
                form.save()
                if form.has_changed():
                    change_data = {k: form.cleaned_data.get(k) for k in form.changed_data}
                    change_data['id'] = form.instance.pk
                    obj.log_action(
                        'pretix.event.question.option.added' if created else
                        'pretix.event.question.option.changed',
                        user=self.request.user, data=change_data
                    )

            return True
        return False

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['formset'] = self.formset
        return ctx


class QuestionView(EventPermissionRequiredMixin, QuestionMixin, ChartContainingView, DetailView):
    model = Question
    template_name = 'pretixcontrol/items/question.html'
    permission = 'can_change_items'
    template_name_field = 'question'

    def get_answer_statistics(self):
        opqs = OrderPosition.objects.filter(
            order__event=self.request.event,
        )
        qs = QuestionAnswer.objects.filter(
            question=self.object, orderposition__isnull=False,
        )

        if self.request.GET.get("subevent", "") != "":
            opqs = opqs.filter(subevent=self.request.GET["subevent"])

        s = self.request.GET.get("status", "np")
        if s != "":
            if s == 'o':
                opqs = opqs.filter(order__status=Order.STATUS_PENDING,
                                   order__expires__lt=now().replace(hour=0, minute=0, second=0))
            elif s == 'np':
                opqs = opqs.filter(order__status__in=[Order.STATUS_PENDING, Order.STATUS_PAID])
            elif s == 'pv':
                opqs = opqs.filter(
                    Q(order__status=Order.STATUS_PAID) |
                    Q(order__status=Order.STATUS_PENDING, order__valid_if_pending=True)
                )
            elif s == 'ne':
                opqs = opqs.filter(order__status__in=[Order.STATUS_PENDING, Order.STATUS_EXPIRED])
            else:
                opqs = opqs.filter(order__status=s)

        if s not in (Order.STATUS_CANCELED, ""):
            opqs = opqs.filter(canceled=False)
        if self.request.GET.get("item", "") != "":
            i = self.request.GET.get("item", "")
            opqs = opqs.filter(item_id__in=(i,))

        qs = qs.filter(orderposition__in=opqs)
        op_cnt = opqs.filter(item__in=self.object.items.all()).count()

        if self.object.type == Question.TYPE_FILE:
            qs = [
                {
                    'answer': gettext('File uploaded'),
                    'count': qs.filter(file__isnull=False).count(),
                }
            ]
        elif self.object.type in (Question.TYPE_CHOICE, Question.TYPE_CHOICE_MULTIPLE):
            qs = qs.order_by('options').values('options', 'options__answer') \
                .annotate(count=Count('id')).order_by('-count')
            for a in qs:
                a['alink'] = a['options']
                a['answer'] = str(a['options__answer'])
                del a['options__answer']
        elif self.object.type in (Question.TYPE_TIME, Question.TYPE_DATE, Question.TYPE_DATETIME):
            qs = qs.order_by('answer')
            model_cache = {a.answer: a for a in qs}
            qs = qs.values('answer').annotate(count=Count('id')).order_by('answer')
            for a in qs:
                a['alink'] = a['answer']
                a['answer'] = str(model_cache[a['answer']])
        else:
            qs = qs.order_by('answer').values('answer').annotate(count=Count('id')).order_by('-count')

            if self.object.type == Question.TYPE_BOOLEAN:
                for a in qs:
                    a['alink'] = a['answer']
                    a['answer_bool'] = a['answer'] == 'True'
                    a['answer'] = gettext('Yes') if a['answer'] == 'True' else gettext('No')
            elif self.object.type == Question.TYPE_COUNTRYCODE:
                for a in qs:
                    a['alink'] = a['answer']
                    a['answer'] = Country(a['answer']).name or a['answer']

        r = list(qs)
        total = sum(a['count'] for a in r)
        for a in r:
            a['percentage'] = (a['count'] / total * 100.) if total else 0
            a['percentage_attendees'] = (a['count'] / op_cnt * 100.) if op_cnt else 0
        return r, total

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data()
        ctx['items'] = self.object.items.all()
        stats = self.get_answer_statistics()
        ctx['stats'], ctx['total'] = stats
        return ctx

    def get_object(self, queryset=None) -> Question:
        try:
            return self.request.event.questions.get(
                id=self.kwargs['question']
            )
        except Question.DoesNotExist:
            raise Http404(_("The requested question does not exist."))

    def get_success_url(self) -> str:
        return reverse('control:event.items.questions', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })


class QuestionUpdate(EventPermissionRequiredMixin, QuestionMixin, UpdateView):
    model = Question
    form_class = QuestionForm
    template_name = 'pretixcontrol/items/question_edit.html'
    permission = 'can_change_items'
    context_object_name = 'question'

    def get_object(self, queryset=None) -> Question:
        try:
            return self.request.event.questions.get(
                id=self.kwargs['question']
            )
        except Question.DoesNotExist:
            raise Http404(_("The requested question does not exist."))

    @transaction.atomic
    def form_valid(self, form):
        if form.cleaned_data.get('type') in ('M', 'C'):
            if not self.save_formset(self.get_object()):
                return self.get(self.request, *self.args, **self.kwargs)

        if form.has_changed():
            self.object.log_action(
                'pretix.event.question.reordered', user=self.request.user, data={
                    k: form.cleaned_data.get(k) for k in form.changed_data
                }
            )
        messages.success(self.request, _('Your changes have been saved.'))
        return super().form_valid(form)

    def get_success_url(self) -> str:
        return reverse('control:event.items.questions', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)


class QuestionCreate(EventPermissionRequiredMixin, QuestionMixin, CreateView):
    model = Question
    form_class = QuestionForm
    template_name = 'pretixcontrol/items/question_edit.html'
    permission = 'can_change_items'
    context_object_name = 'question'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['instance'] = Question(event=self.request.event)
        return kwargs

    def get_success_url(self) -> str:
        return reverse('control:event.items.questions', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })

    def get_object(self, **kwargs):
        return None

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)

    @transaction.atomic
    def form_valid(self, form):
        if form.cleaned_data.get('type') in ('M', 'C'):
            if not self.formset.is_valid():
                return self.get(self.request, *self.args, **self.kwargs)

        messages.success(self.request, _('The new question has been created.'))
        ret = super().form_valid(form)
        form.instance.log_action('pretix.event.question.added', user=self.request.user, data=dict(form.cleaned_data))

        if form.cleaned_data.get('type') in ('M', 'C'):
            self.save_formset(form.instance)

        return ret


class QuotaList(PaginationMixin, ListView):
    model = Quota
    context_object_name = 'quotas'
    template_name = 'pretixcontrol/items/quotas.html'

    def get_queryset(self):
        qs = self.request.event.quotas.prefetch_related(
            Prefetch(
                "items",
                queryset=Item.objects.annotate(
                    has_variations=Exists(ItemVariation.objects.filter(item=OuterRef('pk')))
                ),
                to_attr="cached_items"
            ),
            "variations",
            "variations__item",
            Prefetch(
                "subevent",
                queryset=self.request.event.subevents.all()
            )
        )
        if self.request.GET.get("subevent", "") != "":
            s = self.request.GET.get("subevent", "")
            qs = qs.filter(subevent_id=s)

        valid_orders = {
            '-date': ('-subevent__date_from', 'name', 'pk'),
            'date': ('subevent__date_from', '-name', '-pk'),
            'size': ('size', 'name', 'pk'),
            '-size': ('-size', '-name', '-pk'),
            'name': ('name', 'pk'),
            '-name': ('-name', '-pk'),
        }

        if self.request.GET.get("ordering", "-date") in valid_orders:
            qs = qs.order_by(*valid_orders[self.request.GET.get("ordering", "-date")])
        else:
            qs = qs.order_by('name', 'subevent__date_from', 'pk')

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data()

        qa = QuotaAvailability()
        qa.queue(*ctx['quotas'])
        qa.compute()
        for quota in ctx['quotas']:
            quota.cached_avail = qa.results[quota]

        return ctx


class QuotaCreate(EventPermissionRequiredMixin, CreateView):
    model = Quota
    form_class = QuotaForm
    template_name = 'pretixcontrol/items/quota_edit.html'
    permission = 'can_change_items'
    context_object_name = 'quota'

    def get_success_url(self) -> str:
        return reverse('control:event.items.quotas', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })

    @transaction.atomic
    def form_valid(self, form):
        form.instance.event = self.request.event
        messages.success(self.request, _('The new quota has been created.'))
        ret = super().form_valid(form)
        form.instance.log_action('pretix.event.quota.added', user=self.request.user, data=dict(form.cleaned_data))
        return ret

    @cached_property
    def copy_from(self):
        if self.request.GET.get("copy_from") and not getattr(self, 'object', None):
            try:
                return self.request.event.quotas.get(pk=self.request.GET.get("copy_from"))
            except Quota.DoesNotExist:
                pass

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()

        kwargs.setdefault('initial', {})
        if self.copy_from:
            i = modelcopy(self.copy_from)
            i.pk = None
            kwargs['instance'] = i
            kwargs['initial']['itemvars'] = [str(i.pk) for i in self.copy_from.items.all()] + [
                '{}-{}'.format(v.item_id, v.pk) for v in self.copy_from.variations.all()
            ]
        else:
            kwargs['instance'] = Quota(event=self.request.event)
            if 'product' in self.request.GET:
                kwargs['initial']['itemvars'] = self.request.GET.getlist('product')

        return kwargs

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)


class QuotaView(ChartContainingView, DetailView):
    model = Quota
    template_name = 'pretixcontrol/items/quota.html'
    context_object_name = 'quota'

    def get_context_data(self, *args, **kwargs):
        ctx = super().get_context_data()

        qa = QuotaAvailability(full_results=True)
        qa.queue(self.object)
        qa.compute()
        ctx['avail'] = qa.results[self.object]

        data = [
            {
                'label': gettext('Paid orders'),
                'value': qa.count_paid_orders[self.object],
                'sum': True,
            },
            {
                'label': gettext('Pending orders'),
                'value': qa.count_pending_orders[self.object],
                'sum': True,
            },
        ]
        if self.object.release_after_exit:
            data.append({
                'label': gettext('Exit scans'),
                'value': -1 * qa.count_exited_orders[self.object],
                'sum': True,
            })

        data += [
            {
                'label': gettext('Vouchers and waiting list reservations'),
                'value': qa.count_vouchers[self.object],
                'sum': True,
            },
            {
                'label': gettext('Current user\'s carts'),
                'value': qa.count_cart[self.object],
                'sum': True,
            },
        ]

        sum_values = sum([d['value'] for d in data if d['sum']])
        s = self.object.size - sum_values if self.object.size is not None else gettext('Infinite')

        data.append({
            'label': gettext('Available quota'),
            'value': s,
            'sum': False,
            'strong': True
        })
        data.append({
            'label': gettext('Waiting list (pending)'),
            'value': qa.count_waitinglist[self.object],
            'sum': False,
        })

        if self.object.size is not None:
            data.append({
                'label': gettext('Currently for sale'),
                'value': ctx['avail'][1],
                'sum': False,
                'strong': True
            })

        for d in data:
            if isinstance(d.get('value', 0), int) and d.get('value', 0) < 0:
                d['value_abs'] = abs(d['value'])
        ctx['quota_chart_data'] = json.dumps([r for r in data if r.get('sum') and r['value'] >= 0])
        ctx['quota_table_rows'] = list(data)
        ctx['quota_overbooked'] = sum_values - self.object.size if self.object.size is not None else 0

        ctx['has_plugins'] = False
        res = (
            Quota.AVAILABILITY_GONE if self.object.size is not None and self.object.size - sum_values <= 0 else
            Quota.AVAILABILITY_OK,
            self.object.size - sum_values if self.object.size is not None else None
        )
        for recv, resp in quota_availability.send(sender=self.request.event, quota=self.object, result=res,
                                                  count_waitinglist=True):
            if resp != res:
                ctx['has_plugins'] = True

        ctx['has_ignore_vouchers'] = Voucher.objects.filter(
            Q(allow_ignore_quota=True) &
            Q(Q(valid_until__isnull=True) | Q(valid_until__gte=now())) &
            Q(
                (
                    (  # Orders for items which do not have any variations
                        Q(variation__isnull=True) &
                        Q(item_id__in=Quota.items.through.objects.filter(quota_id=self.object.pk).values_list('item_id', flat=True))
                    ) | (  # Orders for items which do have any variations
                        Q(variation__in=Quota.variations.through.objects.filter(quota_id=self.object.pk).values_list(
                            'itemvariation_id', flat=True))
                    )
                ) | Q(quota=self.object)
            ) &
            Q(redeemed__lt=F('max_usages'))
        ).exists()
        if self.object.closed:
            qa = QuotaAvailability(ignore_closed=True)
            qa.queue(self.object)
            qa.compute()
            ctx['closed_and_sold_out'] = qa.results[self.object][0] <= Quota.AVAILABILITY_ORDERED

        return ctx

    def get_object(self, queryset=None) -> Quota:
        try:
            return self.request.event.quotas.get(
                id=self.kwargs['quota']
            )
        except Quota.DoesNotExist:
            raise Http404(_("The requested quota does not exist."))

    def post(self, request, *args, **kwargs):
        if not request.user.has_event_permission(request.organizer, request.event, 'can_change_items', request):
            raise PermissionDenied()
        quota = self.get_object()
        if 'reopen' in request.POST:
            quota.closed = False
            quota.save(update_fields=['closed'])
            quota.log_action('pretix.event.quota.opened', user=request.user)
            messages.success(request, _('The quota has been re-opened.'))
        if 'disable' in request.POST:
            quota.closed = False
            quota.close_when_sold_out = False
            quota.save(update_fields=['closed', 'close_when_sold_out'])
            quota.log_action('pretix.event.quota.opened', user=request.user)
            quota.log_action(
                'pretix.event.quota.changed', user=self.request.user, data={
                    'close_when_sold_out': False
                }
            )
            messages.success(request, _('The quota has been re-opened and will not close again.'))
        return redirect(reverse('control:event.items.quotas.show', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
            'quota': quota.pk
        }))


class QuotaUpdate(EventPermissionRequiredMixin, UpdateView):
    model = Quota
    form_class = QuotaForm
    template_name = 'pretixcontrol/items/quota_edit.html'
    permission = 'can_change_items'
    context_object_name = 'quota'

    def get_context_data(self, *args, **kwargs):
        ctx = super().get_context_data()
        return ctx

    def get_object(self, queryset=None) -> Quota:
        try:
            return self.request.event.quotas.get(
                id=self.kwargs['quota']
            )
        except Quota.DoesNotExist:
            raise Http404(_("The requested quota does not exist."))

    @transaction.atomic
    def form_valid(self, form):
        messages.success(self.request, _('Your changes have been saved.'))
        if form.has_changed():
            self.object.log_action(
                'pretix.event.quota.changed', user=self.request.user, data={
                    k: form.cleaned_data.get(k) for k in form.changed_data
                }
            )
            if ((form.initial.get('subevent') and not form.instance.subevent) or
                    (form.instance.subevent and form.initial.get('subevent') != form.instance.subevent.pk)):

                if form.initial.get('subevent'):
                    se = SubEvent.objects.get(event=self.request.event, pk=form.initial.get('subevent'))
                    se.log_action(
                        'pretix.subevent.quota.deleted', user=self.request.user, data={
                            'id': form.instance.pk
                        }
                    )
                if form.instance.subevent:
                    form.instance.subevent.log_action(
                        'pretix.subevent.quota.added', user=self.request.user, data={
                            'id': form.instance.pk
                        }
                    )
            form.instance.rebuild_cache()
        return super().form_valid(form)

    def get_success_url(self) -> str:
        return reverse('control:event.items.quotas.show', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
            'quota': self.object.pk
        })

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)


class QuotaDelete(EventPermissionRequiredMixin, CompatDeleteView):
    model = Quota
    template_name = 'pretixcontrol/items/quota_delete.html'
    permission = 'can_change_items'
    context_object_name = 'quota'

    def get_object(self, queryset=None) -> Quota:
        try:
            return self.request.event.quotas.get(
                id=self.kwargs['quota']
            )
        except Quota.DoesNotExist:
            raise Http404(_("The requested quota does not exist."))

    def get_context_data(self, *args, **kwargs) -> dict:
        context = super().get_context_data(*args, **kwargs)
        context['dependent'] = list(self.object.items.all())
        context['vouchers'] = self.object.vouchers.count()
        return context

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        success_url = self.get_success_url()
        self.object.log_action(action='pretix.event.quota.deleted', user=request.user)
        self.object.delete()
        messages.success(self.request, _('The selected quota has been deleted.'))
        return HttpResponseRedirect(success_url)

    def get_success_url(self) -> str:
        return reverse('control:event.items.quotas', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })


class ItemDetailMixin(SingleObjectMixin):
    model = Item
    context_object_name = 'item'

    def get_object(self, queryset=None) -> Item:
        try:
            if not hasattr(self, 'object') or not self.object:
                self.item = self.request.event.items.get(
                    id=self.kwargs['item']
                )
                self.object = self.item
            return self.object
        except Item.DoesNotExist:
            raise Http404(_("The requested item does not exist."))


class MetaDataEditorMixin:
    meta_form = ItemMetaValueForm
    meta_model = ItemMetaValue

    @cached_property
    def meta_forms(self):
        if getattr(self, 'object', None):
            val_instances = {
                v.property_id: v for v in self.object.meta_values.all()
            }
        else:
            val_instances = {}

        if getattr(self, 'copy_from', None):
            defaults = {
                v.property_id: v.value for v in self.copy_from.meta_values.all()
            }
        else:
            defaults = {}

        formlist = []

        for p in self.request.event.item_meta_properties.all():
            formlist.append(self._make_meta_form(p, val_instances, defaults))
        return formlist

    def _make_meta_form(self, p, val_instances, defaults):
        return self.meta_form(
            prefix='prop-{}'.format(p.pk),
            property=p,
            instance=val_instances.get(
                p.pk,
                self.meta_model(
                    property=p,
                    item=self.object if getattr(self, 'object', None) else None,
                    value=defaults.get(p.pk, None)
                )
            ),
            data=(self.request.POST if self.request.method == "POST" else None)
        )

    def save_meta(self):
        for f in self.meta_forms:
            if f.cleaned_data.get('value'):
                if not f.instance.item_id:
                    f.instance.item = self.object
                f.save()
            elif f.instance and f.instance.pk:
                f.instance.delete()


class ItemCreate(EventPermissionRequiredMixin, MetaDataEditorMixin, CreateView):
    form_class = ItemCreateForm
    template_name = 'pretixcontrol/item/create.html'
    permission = 'can_change_items'

    def get_success_url(self) -> str:
        return reverse('control:event.item', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
            'item': self.object.id,
        })

    @cached_property
    def copy_from(self):
        if self.request.GET.get("copy_from") and not getattr(self, 'object', None):
            try:
                return self.request.event.items.get(pk=self.request.GET.get("copy_from"))
            except Item.DoesNotExist:
                pass

    def get_initial(self):
        initial = super().get_initial()
        trs = list(self.request.event.tax_rules.all())
        if len(trs) == 1:
            initial['tax_rule'] = trs[0]

        if self.copy_from:
            fields = ('name', 'internal_name', 'category', 'admission', 'personalized', 'default_price', 'tax_rule')
            for f in fields:
                initial[f] = getattr(self.copy_from, f)
            initial['copy_from'] = self.copy_from
            initial['has_variations'] = self.copy_from.variations.exists()

        return initial

    @transaction.atomic
    def form_valid(self, form):
        messages.success(self.request, _('Your changes have been saved.'))

        ret = super().form_valid(form)
        self.save_meta()
        form.instance.log_action('pretix.event.item.added', user=self.request.user, data={
            k: (form.cleaned_data.get(k).name
                if isinstance(form.cleaned_data.get(k), File)
                else form.cleaned_data.get(k))
            for k in form.changed_data
        })
        return ret

    def get_form_kwargs(self):
        """
        Returns the keyword arguments for instantiating the form.
        """
        newinst = Item(event=self.request.event)
        kwargs = super().get_form_kwargs()
        kwargs.update({'instance': newinst, 'user': self.request.user})
        return kwargs

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data()
        ctx['meta_forms'] = self.meta_forms
        return ctx

    def post(self, request, *args, **kwargs):
        self.object = None
        form = self.get_form()
        if form.is_valid() and all([f.is_valid() for f in self.meta_forms]):
            return self.form_valid(form)
        else:
            return self.form_invalid(form)


class ItemUpdateGeneral(ItemDetailMixin, EventPermissionRequiredMixin, MetaDataEditorMixin, UpdateView):
    form_class = ItemUpdateForm
    template_name = 'pretixcontrol/item/index.html'
    permission = 'can_change_items'

    @cached_property
    def plugin_forms(self):
        forms = []
        for rec, resp in item_forms.send(sender=self.request.event, item=self.item, request=self.request):
            if not resp:
                continue
            if isinstance(resp, (list, tuple)):
                forms.extend(resp)
            else:
                forms.append(resp)

        for form in forms:
            if has_truthy_attr(form, "title") and has_truthy_attr(form, "is_layout"):
                raise ValueError("`title` and `is_layout` must not both be truthy values")

        return forms

    def get_success_url(self) -> str:
        return reverse('control:event.item', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
            'item': self.get_object().id,
        })

    def is_valid(self, form):
        v = (
            form.is_valid()
            and all(f.is_valid() for f in self.plugin_forms)
            and all(f.is_valid() for f in self.formsets.values())
        )
        if v and form.cleaned_data['category'] and form.cleaned_data['category'].is_addon:
            addons = self.formsets['addons'].ordered_forms + [
                ef for ef in self.formsets['addons'].extra_forms
                if ef not in self.formsets['addons'].ordered_forms and ef not in self.formsets['addons'].deleted_forms
            ]
            if addons:
                messages.error(self.request,
                               _('You cannot add add-ons to a product that is only available as an add-on '
                                 'itself.'))
                v = False

            bundles = [
                ef for ef in self.formsets['bundles'].forms
                if ef not in self.formsets['bundles'].deleted_forms
            ]
            if bundles:
                messages.error(self.request,
                               _('You cannot add bundles to a product that is only available as an add-on '
                                 'itself.'))
                v = False
        return v

    def post(self, request, *args, **kwargs):
        self.get_object()
        form = self.get_form()
        if self.is_valid(form) and all([f.is_valid() for f in self.meta_forms]):
            return self.form_valid(form)
        else:
            return self.form_invalid(form)

    def save_formset(self, key, log_base, attr='item', order=True, serializer=None,
                     rm_verb='removed'):
        for form in self.formsets[key].deleted_forms:
            if not form.instance.pk:
                continue
            d = {
                'id': form.instance.pk
            }
            if serializer:
                d.update(serializer(form.instance, context={'event': self.request.event}).data)
            self.get_object().log_action(
                'pretix.event.item.{}.{}'.format(log_base, rm_verb), user=self.request.user, data=d
            )
            form.instance.delete()
            form.instance.pk = None

        if order:
            forms = self.formsets[key].ordered_forms + [
                ef for ef in self.formsets[key].extra_forms
                if ef not in self.formsets[key].ordered_forms and ef not in self.formsets[key].deleted_forms
            ]
        else:
            forms = [
                ef for ef in self.formsets[key].forms
                if ef not in self.formsets[key].deleted_forms
            ]
        for i, form in enumerate(forms):
            if order:
                form.instance.position = i
            setattr(form.instance, attr, self.get_object())
            created = not form.instance.pk
            form.save()
            if form.has_changed() and any(a for a in form.changed_data if a != 'ORDER'):
                change_data = {k: form.cleaned_data.get(k) for k in form.changed_data}
                if key == 'variations':
                    change_data['value'] = form.instance.value
                change_data['id'] = form.instance.pk
                self.get_object().log_action(
                    'pretix.event.item.{}.changed'.format(log_base) if not created else
                    'pretix.event.item.{}.added'.format(log_base),
                    user=self.request.user, data=change_data
                )

    @transaction.atomic
    def form_valid(self, form):
        self.save_meta()
        messages.success(self.request, _('Your changes have been saved.'))

        change_data = {
            k: form.cleaned_data.get(k)
            for k in form.changed_data
        }
        for f in self.plugin_forms:
            change_data.update({
                k: (f.cleaned_data.get(k).name
                    if isinstance(f.cleaned_data.get(k), File)
                    else f.cleaned_data.get(k))
                for k in f.changed_data
            })

        meta_changed = {}
        for f in self.meta_forms:
            meta_changed.update({
                k: (f.cleaned_data.get(k).name
                    if isinstance(f.cleaned_data.get(k), File)
                    else f.cleaned_data.get(k))
                for k in f.changed_data
            })
        if meta_changed:
            change_data['meta_data'] = meta_changed

        if change_data:
            self.object.log_action(
                'pretix.event.item.changed', user=self.request.user, data=change_data
            )
            invalidate_cache.apply_async(kwargs={'event': self.request.event.pk, 'item': self.object.pk})

        for f in self.plugin_forms:
            f.save()

        for k, v in self.formsets.items():
            if k == 'variations':
                self.save_formset(
                    'variations', 'variation',
                    serializer=ItemVariationSerializer,
                    rm_verb='deleted'
                )
            elif k == 'addons':
                self.save_formset(
                    'addons', 'addons', 'base_item',
                    serializer=ItemAddOnSerializer
                )
            elif k == 'bundles':
                self.save_formset(
                    'bundles', 'bundles', 'base_item', order=False,
                    serializer=ItemBundleSerializer
                )
            else:
                v.save()

        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save your changes. See below for details.'))
        return super().form_invalid(form)

    def get_object(self, queryset=None) -> Item:
        o = super().get_object(queryset)
        if o.hide_without_voucher:
            o.require_voucher = True
        return o

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data()
        ctx['plugin_forms'] = self.plugin_forms
        ctx['meta_forms'] = self.meta_forms
        ctx['formsets'] = self.formsets

        if not ctx['item'].active and ctx['item'].bundled_with.count() > 0:
            messages.info(self.request, _("You disabled this item, but it is still part of a product bundle. "
                                          "Your participants won't be able to buy the bundle unless you remove this "
                                          "item from it."))

        ctx['sales_channels'] = self.request.organizer.sales_channels.all()
        return ctx

    @cached_property
    def formsets(self):
        f = OrderedDict([
            ('variations', inlineformset_factory(
                Item, ItemVariation,
                form=ItemVariationForm, formset=ItemVariationsFormSet,
                can_order=True, can_delete=True, extra=0
            )(
                self.request.POST if self.request.method == "POST" else None,
                queryset=ItemVariation.objects.filter(item=self.get_object()).prefetch_related(
                    'meta_values', 'limit_sales_channels', 'require_membership_types'
                ),
                event=self.request.event, prefix="variations"
            )),
            ('addons', inlineformset_factory(
                Item, ItemAddOn,
                form=ItemAddOnForm, formset=ItemAddOnsFormSet,
                can_order=True, can_delete=True, extra=0
            )(
                self.request.POST if self.request.method == "POST" else None,
                queryset=ItemAddOn.objects.filter(base_item=self.get_object()),
                event=self.request.event, prefix="addons"
            )),
            ('bundles', inlineformset_factory(
                Item, ItemBundle,
                form=ItemBundleForm, formset=ItemBundleFormSet,
                fk_name='base_item',
                can_order=False, can_delete=True, extra=0
            )(
                self.request.POST if self.request.method == "POST" else None,
                queryset=ItemBundle.objects.filter(base_item=self.get_object()),
                event=self.request.event, item=self.item, prefix="bundles"
            )),
        ])
        if not self.object.has_variations:
            del f['variations']

        i = 0
        for rec, resp in item_formsets.send(sender=self.request.event, item=self.item, request=self.request):
            if isinstance(resp, (list, tuple)):
                for k in resp:
                    f['p-{}'.format(i)] = k
                    i += 1
            else:
                f['p-{}'.format(i)] = resp
                i += 1
        return f


class ItemDelete(EventPermissionRequiredMixin, CompatDeleteView):
    model = Item
    template_name = 'pretixcontrol/item/delete.html'
    permission = 'can_change_items'
    context_object_name = 'item'

    def get_context_data(self, *args, **kwargs) -> dict:
        context = super().get_context_data(*args, **kwargs)
        context['possible'] = self.is_allowed()
        context['vouchers'] = self.object.vouchers.count()
        return context

    def is_allowed(self) -> bool:
        return not self.get_object().orderposition_set.exists()

    def get_object(self, queryset=None) -> Item:
        if not hasattr(self, 'object') or not self.object:
            try:
                self.object = self.request.event.items.get(
                    id=self.kwargs['item']
                )
            except Item.DoesNotExist:
                raise Http404(_("The requested product does not exist."))
        return self.object

    @transaction.atomic
    def delete(self, request, *args, **kwargs):
        success_url = self.get_success_url()
        o = self.get_object()
        if o.allow_delete():
            try:
                CartPosition.objects.filter(addon_to__item=self.get_object()).delete()
                self.get_object().cartposition_set.all().delete()
                self.get_object().log_action('pretix.event.item.deleted', user=self.request.user)
                self.get_object().delete()
            except ProtectedError:
                o = self.get_object()
                o.active = False
                o.save()
                o.log_action('pretix.event.item.changed', user=self.request.user, data={
                    'active': False
                })
                messages.error(self.request, _('The product could not be deleted as some constraints (e.g. data created by '
                                               'plug-ins) did not allow it. Deleting it could break reporting or other '
                                               'functionality, so the product has been disabled instead.'))
            else:
                messages.success(request, _('The selected product has been deleted.'))
            return HttpResponseRedirect(success_url)
        else:
            o = self.get_object()
            o.active = False
            o.save()
            o.log_action('pretix.event.item.changed', user=self.request.user, data={
                'active': False
            })
            messages.success(request, _('The selected product has been deactivated.'))
            return HttpResponseRedirect(success_url)

    def get_success_url(self) -> str:
        return reverse('control:event.items', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug,
        })
