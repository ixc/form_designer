import sys

from django import forms
from django.conf import settings
from django.core.mail import send_mail
from django.db import models
from django.db.models.fields import BLANK_CHOICE_DASH
from django.template import RequestContext
from django.template.loader import render_to_string
from django.template.defaultfilters import slugify
from django.utils.datastructures import SortedDict
from django.utils.functional import curry
from django.utils.translation import ugettext_lazy as _

from form_designer.utils import JSONFieldDescriptor
from django.core.exceptions import ImproperlyConfigured

# Parse the form classes specified in the settings, which specify which classes
# the form should inherit from
if hasattr(settings, 'FORM_DESIGNER_CLASSES'):
    FORM_CLASSES = ()
    form_subclassed = False
    for form_class in settings.FORM_DESIGNER_CLASSES:
        if isinstance(form_class, type):
            FORM_CLASSES += (form_class,)
        elif isinstance(form_class, basestring):
            # If we catch any errors here, the resulting error messages will
            # be unhelpful for debugging, so we'll just let them occur
            module, klass = form_class.rsplit('.', 1)
            __import__(module)
            FORM_CLASSES += (getattr(sys.modules[module], klass),)
        else:
            raise ImproperlyConfigured('FORM_DESIGNER_CLASSES must be a tuple of either classes or class import path strings.')
        # Check if any of the classes parsed so far inherit from Form
        form_subclassed = form_subclassed or isinstance(FORM_CLASSES[-1], forms.Form)
    if not form_subclassed:
        # If none of the specified classes inherit from Form, apped it to the
        # list of classes to inherit from
        FORM_CLASSES += (forms.Form,)
else:
    FORM_CLASSES = (forms.Form,)

FORM_TAKES_REQUEST = getattr(settings, 'FORM_DESIGNER_PASS_REQUEST', False)
HIDDEN_FIELDS = getattr(settings, 'FORM_DESIGNER_HIDDEN_FIELDS', [])

class Form(models.Model):
    CONFIG_OPTIONS = [
        ('email', {
            'title': _('E-mail'),
            'form_fields': [
                ('email', forms.EmailField(_('e-mail address'))),
            ],
        }),
    ]

    title = models.CharField(_('title'), max_length=100)

    config_json = models.TextField(_('config'), blank=True)
    config = JSONFieldDescriptor('config_json')

    class Meta:
        verbose_name = _('form')
        verbose_name_plural = _('forms')

    def __unicode__(self):
        return self.title

    def form(self):
        fields = SortedDict()

        for field in self.fields.all():
            field.add_formfield(fields, self)

        return type('Form%s' % self.pk, FORM_CLASSES, fields)

    def process(self, form, request):
        submission = FormSubmission.objects.create(
            form=self, data=repr(form.cleaned_data), path=request.path)
        
        if 'email' in self.config:
            send_mail(self.title, submission.formatted_data(),
                      settings.DEFAULT_FROM_EMAIL,
                      [self.config['email']['email']], fail_silently=True)
            return _('Thank you, your input has been received.')
        

class FormField(models.Model):
    FIELD_TYPES = [
        ('text', _('text'), forms.CharField),
        ('email', _('e-mail address'), forms.EmailField),
        ('longtext', _('long text'),
         curry(forms.CharField, widget=forms.Textarea)),
        ('checkbox', _('checkbox'), curry(forms.BooleanField, required=False)),
        ('select', _('select'), curry(forms.ChoiceField, required=False)),
        ('radio', _('radio'),
         curry(forms.ChoiceField, widget=forms.RadioSelect)),
        ('multiple-select', _('multiple select'), forms.MultipleChoiceField),
    ]

    form = models.ForeignKey(Form, related_name='fields',
        verbose_name=_('form'))
    ordering = models.IntegerField(_('ordering'), default=0)

    title = models.CharField(_('title'), max_length=100)
    name = models.CharField(_('name'), max_length=100)
    type = models.CharField(
        _('type'), max_length=20, choices=[r[:2] for r in FIELD_TYPES])
    choices = models.CharField(
        _('choices'), max_length=1024, blank=True,
        help_text=_('Comma-separated'))
    help_text = models.CharField(
        _('help text'), max_length=1024, blank=True,
        help_text=_('Optional extra explanatory text beside the field'))
    
    is_required = models.BooleanField(_('is required'), default=True)

    class Meta:
        ordering = ['ordering', 'id']
        unique_together = (('form', 'name'),)
        verbose_name = _('form field')
        verbose_name_plural = _('form fields')

    def __unicode__(self):
        return self.title

    def clean(self):
        if self.choices and not isinstance(self.get_type(), forms.ChoiceField):
            raise forms.ValidationError(
                _("You can't specify choices for %s fields") % self.type)
    
    def get_choices(self):
        get_tuple = lambda value: (slugify(value.strip()), value.strip())
        choices = [get_tuple(value) for value in self.choices.split(',')]
        if not self.is_required and self.type=='select':
            choices = BLANK_CHOICE_DASH + choices
        return tuple(choices)

    def get_type(self, **kwargs):
        types = dict((r[0], r[2]) for r in self.FIELD_TYPES)
        return types[self.type](**kwargs)
    
    def add_formfield(self, fields, form):
        fields[self.name] = self.formfield()

    def formfield(self):
        kwargs = dict(label=self.title, required=self.is_required)
        if self.choices:
            kwargs['choices'] = self.get_choices()
        if self.help_text:
            kwargs['help_text'] = self.help_text
        return self.get_type(**kwargs)


class FormSubmission(models.Model):
    submitted = models.DateTimeField(auto_now_add=True)
    form = models.ForeignKey(
        Form, verbose_name=_('form'), related_name='submissions')
    data = models.TextField()
    path = models.CharField(max_length=255)

    class Meta:
        ordering = ('-submitted',)

    def sorted_data(self, include=()):
        """ Return SortedDict by field ordering and using titles as keys.

        `include` can be a tuple containing any or all of 'date', 'time',
        'datetime', or 'path' to include additional meta data.
        """
        data_dict = eval(self.data)
        data = SortedDict()
        field_names = []
        for field in self.form.fields.all():
            data[field.title] = data_dict.get(field.name)
            field_names.append(field.name)
        # append any extra data (form may have changed since submission, etc)
        for field_name in data_dict:
            if not field_name in field_names:
                data[field_name] = data_dict[field_name]
        if 'datetime' in include:
            data['submitted'] = self.submitted
        if 'date' in include:
            data['date submitted'] = self.submitted.date()
        if 'time' in include:
            data['time submitted'] = self.submitted.time()
        if 'path' in include:
            data['form path'] = self.path
        return data
        
    def formatted_data(self, html=False):
        formatted = ""
        for key, value in self.sorted_data().items():
            if key not in HIDDEN_FIELDS:
                if html:
                    formatted += "<dt>%s</dt><dd>%s</dd>\n" % (key, value)
                else:
                    formatted += "%s: %s\n" % (key, value)
        return formatted if not html else "<dl>%s</dl>" % formatted

    def formatted_data_html(self):
        return self.formatted_data(html=True)
        

class FormContent(models.Model):
    form = models.ForeignKey(Form, verbose_name=_('form'),
                             related_name='%(app_label)s_%(class)s_related')
    show_form_title = models.BooleanField(_('show form title'), default=True)
    success_message = models.TextField(
        _('success message'), blank=True, help_text=
        _("Optional custom message to display after valid form is submitted"))

    template = 'content/form/form.html'
    
    class Meta:
        abstract = True
        verbose_name = _('form')
        verbose_name_plural = _('forms')

    def process_valid_form(self, request, form_instance, **kwargs):
        """ Process form and return response (hook method). """
        process_result = self.form.process(form_instance, request)
        context = RequestContext(
            request, {
                'content': self,
                'message': self.success_message or process_result or u''})
        return render_to_string(self.template, context)
        
    def render(self, request, **kwargs):
        if FORM_TAKES_REQUEST:
            form_class = lambda *args, **kwargs: self.form.form()(request, *args, **kwargs)
        else:
            form_class = self.form.form()
        prefix = 'fc%d' % self.id

        if request.method == 'POST':
            form_instance = form_class(request.POST, prefix=prefix)

            if form_instance.is_valid():
                return self.process_valid_form(request, form_instance, **kwargs)
        else:
            form_instance = form_class(prefix=prefix)

        context = RequestContext(
            request, {'content': self, 'form': form_instance})
        return render_to_string(self.template, context)
