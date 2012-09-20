import types
from django import forms
from django.core.validators import EMPTY_VALUES
from django.utils.datastructures import SortedDict
from mongoengine.base import BaseDocument
from fields import MongoFormFieldGenerator
from utils import mongoengine_validate_wrapper, iter_valid_fields
from mongoengine.fields import ReferenceField
from django.forms.formsets import BaseFormSet, formset_factory


__all__ = ('MongoForm', 
           'BaseDocumentFormSet',
           'documentform_factory',
           'documentformset_factory')

class MongoFormMetaClass(type):
    """Metaclass to create a new MongoForm."""

    def __new__(cls, name, bases, attrs):

        # get all valid existing Fields and sort them
        fields = [(field_name, attrs.pop(field_name)) for field_name, obj in \
            attrs.items() if isinstance(obj, forms.Field)]
        fields.sort(lambda x, y: cmp(x[1].creation_counter, y[1].creation_counter))

        # get all Fields from base classes
        for base in bases[::-1]:
            if hasattr(base, 'base_fields'):
                fields = base.base_fields.items() + fields

        # add the fields as "our" base fields
        attrs['base_fields'] = SortedDict(fields)
        
        # Meta class available?
        if 'Meta' in attrs and hasattr(attrs['Meta'], 'document') and \
           issubclass(attrs['Meta'].document, BaseDocument):
            doc_fields = SortedDict()

            formfield_generator = getattr(attrs['Meta'], 'formfield_generator', \
                MongoFormFieldGenerator)()


            # import pdb;pdb.set_trace()
            # if cls.__name__ == 'Channels':

            # walk through the document fields
            for field_name, field in iter_valid_fields(attrs['Meta']):
                # add field and override clean method to respect mongoengine-validator
                _field = formfield_generator.generate(field_name, field)
                if isinstance(_field, dict):
                    for f in _field.itervalues():
                        fname = f.label.lower()
                        doc_fields[fname] = f
                else:
                    doc_fields[field_name] = _field
                    doc_fields[field_name].clean = mongoengine_validate_wrapper(
                        doc_fields[field_name].clean, field._validate)

            # write the new document fields to base_fields
            doc_fields.update(attrs['base_fields'])
            attrs['base_fields'] = doc_fields

        # maybe we need the Meta class later
        attrs['_meta'] = attrs.get('Meta', object())

        return super(MongoFormMetaClass, cls).__new__(cls, name, bases, attrs)

class MongoForm(forms.BaseForm):
    """Base MongoForm class. Used to create new MongoForms"""
    __metaclass__ = MongoFormMetaClass

    def __init__(self, data=None, files=None, auto_id='id_%s', prefix=None,
        initial=None, error_class=forms.util.ErrorList, label_suffix=':',
        empty_permitted=False, instance=None):

        """ initialize the form"""
        assert isinstance(instance, (types.NoneType, BaseDocument)), \
            'instance must be a mongoengine document, not %s' % \
                type(instance).__name__

        assert hasattr(self, 'Meta'), 'Meta class is needed to use MongoForm'
        # new instance or updating an existing one?
        if instance is None:
            if self._meta.document is None:
                raise ValueError('MongoForm has no document class specified.')
            self.instance = self._meta.document()
            object_data = {}
            self.instance._adding = True
        else:
            self.instance = instance
            self.instance._adding = False
            object_data = {}

            # walk through the document fields
            for field_name, field in iter_valid_fields(self._meta):
                # add field data if needed
                if not hasattr(instance, field_name):
                    continue
                field_data = getattr(instance, field_name)
                if not self._meta.document._dynamic:
                    fields = self._meta.document._fields
                # add dfields if document is dynamic
                elif hasattr(self._meta.document, '_dfields'):
                    fields = self._meta.document._dfields
                else:
                    continue
                if field_name in fields:
                    _field = fields[field_name]
                    if isinstance(_field, ReferenceField):
                        field_data = field_data and str(field_data.id)
                object_data[field_name] = field_data
        # additional initial data available?
        if initial is not None:
            object_data.update(initial)

        self._validate_unique = False
        super(MongoForm, self).__init__(data, files, auto_id, prefix,
            object_data, error_class, label_suffix, empty_permitted)

    def _get_validation_exclusions(self):
        """
        For backwards-compatibility, several types of fields need to be
        excluded from model validation. See the following tickets for
        details: #12507, #12521, #12553
        """
        exclude = []
        # Build up a list of fields that should be excluded from model field
        # validation and unique checks.
        for f in self.instance._fields.itervalues():
            field = f.name
            # Exclude fields that aren't on the form. The developer may be
            # adding these values to the model after form validation.
            if field not in self.fields:
                exclude.append(f.name)
            elif field in self._errors.keys():
                exclude.append(f.name)

            # Exclude empty fields that are not required by the form, if the
            # underlying model field is required. This keeps the model field
            # from raising a required error. Note: don't exclude the field from
            # validaton if the model field allows blanks. If it does, the blank
            # value may be included in a unique check, so cannot be excluded
            # from validation.
            else:
                field_value = self.cleaned_data.get(field, None)
                if not f.required and field_value in EMPTY_VALUES:
                    exclude.append(f.name)
        return exclude

    def validate_unique(self):
        """
        Validates unique constrains on the document.
        unique_with is not checked at the moment.
        """
        errors = []
        exclude = self._get_validation_exclusions()
        for f in self.instance._fields.itervalues():
            if f.unique and f.name not in exclude:
                filter_kwargs = {
                    f.name: getattr(self.instance, f.name)
                }
                qs = self.instance.__class__.objects().filter(**filter_kwargs)
                # Exclude the current object from the query if we are editing an
                # instance (as opposed to creating a new one)
                if self.instance.pk is not None:
                    qs = qs.filter(pk__ne=self.instance.pk)
                if len(qs) > 0:
                    message = _(u"%(model_name)s with this %(field_label)s already exists.") %  {
                                'model_name': unicode(capfirst(self.instance._meta.verbose_name)),
                                'field_label': unicode(pretty_name(f.name))
                    }
                    err_dict = {f.name: [message]}
                    self._update_errors(err_dict)
                    errors.append(err_dict)
        
        return errors

    def save(self, commit=True):
        """save the instance or create a new one.."""

        # walk through the document fields
        for field_name, field in iter_valid_fields(self._meta):
            setattr(self.instance, field_name, self.cleaned_data.get(field_name))

        if commit:
            self.instance.save()

        return self.instance


def documentform_factory(document, form=MongoForm, fields=None, exclude=None,
                       formfield_callback=None):
    # Build up a list of attributes that the Meta object will have.

    attrs = {'document': document, 'model': document}
    if fields is not None:
        attrs['fields'] = fields
    if exclude is not None:
        attrs['exclude'] = exclude

    # If parent form class already has an inner Meta, the Meta we're
    # creating needs to inherit from the parent's inner meta.
    parent = (object,)
    if hasattr(form, 'Meta'):
        parent = (form.Meta, object)
    Meta = type('Meta', parent, attrs)

    # Give this new form class a reasonable name.
    if isinstance(document, type):
        doc_inst = document()
    else:
        doc_inst = document
    class_name = doc_inst.__class__.__name__ #+ 'Form'

    # Class attributes for the new form class.
    form_class_attrs = {
        'Meta': Meta,
        'formfield_callback': formfield_callback
    }

    return MongoFormMetaClass(class_name, (form,), form_class_attrs)


class BaseDocumentFormSet(BaseFormSet):
    """
    A ``FormSet`` for editing a queryset and/or adding new objects to it.
    """

    def __init__(self, data=None, files=None, auto_id='id_%s', prefix=None,
                 queryset=None, **kwargs):
        self.queryset = queryset
        self._queryset = self.queryset
        self.initial = self.construct_initial()
        prefix = self.document.__name__.lower()
        defaults = {'data': data, 'files': files, 'auto_id': auto_id, 
                    'prefix': prefix, 'initial': self.initial}
        defaults.update(kwargs)
        super(BaseDocumentFormSet, self).__init__(**defaults)

    def construct_initial(self):
        initial = []
        try:
            for d in self.get_queryset():
                initial.append(document_to_dict(d))
        except TypeError:
            pass 
        return initial

    # NOTE: Temporarily commented.
    # TypeError: "object of type 'NoneType' has no len()

    # def initial_form_count(self):
    #     """Returns the number of forms that are required in this FormSet."""
    #     if not (self.data or self.files):
    #         return len(self.get_queryset())
    #     return super(BaseDocumentFormSet, self).initial_form_count()

    def get_queryset(self):
        return self._queryset

    def save_object(self, form, commit=False):
        obj = form.save(commit=commit)
        return obj

    def save(self, commit=True):
        """
        Saves model instances for every form, adding and changing instances
        as necessary, and returns the list of instances.
        """ 
        saved = []
        for form in self.forms:
            if not form.has_changed() and not form in self.initial_forms:
                continue
            obj = self.save_object(form, commit)
            saved.append(obj)
        return saved

    def clean(self):
        self.validate_unique()

    def validate_unique(self):
        errors = []
        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue    
            errors += form.validate_unique()
            
        if errors:
            raise ValidationError(errors)

    def get_date_error_message(self, date_check):
        return ugettext("Please correct the duplicate data for %(field_name)s "
            "which must be unique for the %(lookup)s in %(date_field)s.") % {
            'field_name': date_check[2],
            'date_field': date_check[3],
            'lookup': unicode(date_check[1]),
        }

    def get_form_error(self):
        return ugettext("Please correct the duplicate values below.")

def documentformset_factory(document, form=MongoForm, formfield_callback=None,
                         formset=BaseDocumentFormSet, prefix=None,
                         extra=1, can_delete=False, can_order=False,
                         max_num=None, fields=None, exclude=None):
    """
    Returns a FormSet class for the given Django model class.
    """
    form = documentform_factory(document, form=form, fields=fields, exclude=exclude,
                             formfield_callback=formfield_callback)
    FormSet = formset_factory(form, formset, extra=extra, max_num=max_num,
                              can_order=can_order, can_delete=can_delete)
    FormSet.model = document
    FormSet.document = document
    return FormSet



