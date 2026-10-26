from django import forms


class FightSearchForm(forms.Form):
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    system = forms.CharField(required=False, max_length=100, label="System or region")
    who = forms.CharField(required=False, max_length=100, label="Character, corp or alliance")
    ship = forms.CharField(required=False, max_length=100, label="Ship type")
    uploader = forms.CharField(required=False, max_length=100)
    text = forms.CharField(required=False, max_length=100, label="Title, notes or tag")
    min_pilots = forms.IntegerField(required=False, min_value=0, label="Min. pilots")
    ours = forms.BooleanField(required=False, label="Our side involved")


class FriendlyEntityForm(forms.Form):
    entity_type = forms.ChoiceField(choices=[("alliance", "Alliance"), ("corporation", "Corporation")])
    identifier = forms.CharField(max_length=100, help_text="ID or exact name")
