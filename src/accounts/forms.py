from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm

from accounts.models import User


class ClaimingPasswordResetForm(PasswordResetForm):
    """Also sends links to accounts that never had a password.

    Everyone imported from a fixture file (every participant and judge in fixtures.json)
    starts without one. Django's form skips such accounts; here the reset link doubles
    as "claim your account", which is how an imported person gets in.
    """

    def get_users(self, email):
        email = (email or "").strip().lower()
        return (u for u in User.objects.filter(email__iexact=email, is_active=True))


class LoginForm(AuthenticationForm):
    username = forms.EmailField(
        label="Email", widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"})
    )


class SignupForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="At least 10 characters, and not a common password.",
    )

    class Meta:
        model = User
        fields = ["name", "email"]
        widgets = {"email": forms.EmailInput(attrs={"autocomplete": "email"})}

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"])
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("An account with this email already exists. Sign in instead.")
        return email

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            candidate = User(email=cleaned.get("email", ""), name=cleaned.get("name", ""))
            try:
                password_validation.validate_password(password, candidate)
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["name"]


class TokenForm(forms.Form):
    name = forms.CharField(max_length=80, help_text="What this token is for, e.g. \"results bot\".")
    expires_in_days = forms.TypedChoiceField(
        coerce=int,
        choices=[(30, "30 days"), (90, "90 days"), (365, "1 year"), (0, "Never")],
        initial=90,
    )
