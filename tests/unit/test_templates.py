from app.web.routes import templates


def test_all_templates_compile():
    for name in templates.env.list_templates():
        templates.get_template(name)


def test_demo_email_validation():
    from app.schemas.requests import Login

    assert Login(email="staff@slotrescue.test", password="x").email == "staff@slotrescue.test"
