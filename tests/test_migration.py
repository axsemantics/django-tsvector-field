from django.db import connection, migrations, models
from django.db.migrations.state import ProjectState
from django.db.migrations.writer import MigrationWriter
from django.test import TestCase
from django.test.utils import isolate_apps

from tsvector import SearchVectorField, WeightedColumn
from tsvector.migrate import inject_trigger_operations
from tsvector.schema import TriggerSchemaEditor


@isolate_apps('tests')
class MigrationWriterTests(TestCase):

    def test_deconstruct_with_no_arguments(self):
        svf = SearchVectorField()
        self.assertEqual(
            ("tsvector.SearchVectorField()",
             {'import tsvector'}),
            MigrationWriter.serialize(svf)
        )

    def test_deconstruct_default_arguments(self):

        svf = SearchVectorField([
            WeightedColumn('name', 'A'),
            WeightedColumn('description', 'D'),
        ], language=None, language_column=None, force_update=False)

        definition, path = MigrationWriter.serialize(svf)

        self.assertEqual(
            "tsvector.SearchVectorField("
            "columns=["
            "tsvector.WeightedColumn('name', 'A'), "
            "tsvector.WeightedColumn('description', 'D')]"
            ")",
            definition
        )

        self.assertSetEqual(
            {'import tsvector'},
            path
        )

    def test_deconstruct_all_arguments(self):

        class TextDocument(models.Model):
            svf = SearchVectorField([
                WeightedColumn('name', 'A'),
                WeightedColumn('description', 'D'),
            ], language='english', language_column='lang', force_update=True)

        name, path, args, kwargs = TextDocument._meta.get_field('svf').deconstruct()

        self.assertEqual(name, "svf")
        self.assertEqual(path, "tsvector.SearchVectorField")
        self.assertFalse(args)
        self.assertSetEqual(set(kwargs.keys()), {
            'columns', 'language', 'language_column', 'force_update'
        })


@isolate_apps('tests')
class SchemaEditorTests(TestCase):

    def test_sql_setweight(self):

        def check_sql(model, sql):
            trigger_editor = TriggerSchemaEditor(connection)
            field = model._meta.get_field('search')
            self.assertEqual(
                sql, trigger_editor._tsvector_setweight(field)
            )

        class WithLanguageTwoColumn(models.Model):
            search = SearchVectorField([
                WeightedColumn('title', 'A'),
                WeightedColumn('body', 'D'),
            ], language='ukrainian')

        class WithLanguage(models.Model):
            search = SearchVectorField([
                WeightedColumn('body', 'D'),
            ], language='ukrainian')

        class WithLanguageColumn(models.Model):
            search = SearchVectorField([
                WeightedColumn('body', 'D'),
            ], language_column='lang')

        class WithLanguageAndLanguageColumn(models.Model):
            search = SearchVectorField([
                WeightedColumn('body', 'D'),
            ], language='ukrainian', language_column='lang')

        check_sql(
            WithLanguageTwoColumn, [
                """setweight(to_tsvector('ukrainian', COALESCE(NEW."title", '')), 'A') ||""",
                """setweight(to_tsvector('ukrainian', COALESCE(NEW."body", '')), 'D');"""
            ]
        )

        check_sql(
            WithLanguage, [
                """setweight(to_tsvector('ukrainian', COALESCE(NEW."body", '')), 'D');"""
            ]
        )

        check_sql(
            WithLanguageColumn, [
                """setweight(to_tsvector(NEW."lang"::regconfig, COALESCE(NEW."body", '')), 'D');"""
            ]
        )

        check_sql(
            WithLanguageAndLanguageColumn, [
                """setweight(to_tsvector(COALESCE(NEW."lang"::regconfig, 'ukrainian'),"""
                """ COALESCE(NEW."body", '')), 'D');"""
            ]
        )

    def test_sql_update_column_checks(self):

        def check_sql(model, sql):
            trigger_editor = TriggerSchemaEditor(connection)
            field = model._meta.get_field('search')
            self.assertEqual(
                sql, trigger_editor._tsvector_update_column_checks(field)
            )

        class OneColumn(models.Model):
            search = SearchVectorField([
                WeightedColumn('name', 'A'),
            ])

        class ThreeColumns(models.Model):
            search = SearchVectorField([
                WeightedColumn('name', 'A'),
                WeightedColumn('title', 'B'),
                WeightedColumn('body', 'C'),
            ])

        check_sql(
            OneColumn, [
                'IF (NEW."name" <> OLD."name") THEN do_update = true;',
                'END IF;'
            ]
        )

        check_sql(
            ThreeColumns, [
                'IF (NEW."name" <> OLD."name") THEN do_update = true;',
                'ELSIF (NEW."title" <> OLD."title") THEN do_update = true;',
                'ELSIF (NEW."body" <> OLD."body") THEN do_update = true;',
                'END IF;'
            ]
        )

    def test_sql_update_function(self):

        def check_sql(model, sql):
            trigger_editor = TriggerSchemaEditor(connection)
            field = model._meta.get_field('search')
            self.assertEqual(
                sql, trigger_editor._create_tsvector_update_function('thefunction', field)
            )

        class TextDocument(models.Model):
            search = SearchVectorField([
                WeightedColumn('title', 'A'),
                WeightedColumn('body', 'D'),
            ], 'english')

        check_sql(
            TextDocument,
            "CREATE FUNCTION thefunction() RETURNS trigger AS $$\n"
            "DECLARE\n"
            " do_update bool default false;\n"
            "BEGIN\n"
            " IF (TG_OP = 'INSERT') THEN do_update = true;\n"
            " ELSIF (TG_OP = 'UPDATE') THEN\n"
            '  IF (NEW."title" <> OLD."title") THEN do_update = true;\n'
            '  ELSIF (NEW."body" <> OLD."body") THEN do_update = true;\n'
            "  END IF;\n"
            " END IF;\n"
            " IF do_update THEN\n"
            '  NEW."search" :=\n'
            "   setweight(to_tsvector('english', COALESCE(NEW.\"title\", '')), 'A') ||\n"
            "   setweight(to_tsvector('english', COALESCE(NEW.\"body\", '')), 'D');\n"
            " END IF;\n"
            " RETURN NEW;\n"
            "END\n"
            "$$ LANGUAGE plpgsql"
        )

        class TextDocumentForceUpdate(models.Model):
            search = SearchVectorField([
                WeightedColumn('body', 'D'),
            ], 'english', force_update=True)

        check_sql(
            TextDocumentForceUpdate,
            "CREATE FUNCTION thefunction() RETURNS trigger AS $$\n"
            "DECLARE\n"
            " do_update bool default false;\n"
            "BEGIN\n"
            " do_update = true;\n"
            " IF do_update THEN\n"
            '  NEW."search" :=\n'
            "   setweight(to_tsvector('english', COALESCE(NEW.\"body\", '')), 'D');\n"
            " END IF;\n"
            " RETURN NEW;\n"
            "END\n"
            "$$ LANGUAGE plpgsql"
        )

    def test_create_model_no_function(self):

        class NoWeightedColumns(models.Model):
            search = SearchVectorField()

        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(NoWeightedColumns)
            self.assertEqual(len(schema_editor.deferred_sql), 1)
            self.assertIn('CREATE INDEX', schema_editor.deferred_sql[0])

        with TriggerSchemaEditor(connection) as schema_editor:
            schema_editor.create_model(NoWeightedColumns)
            self.assertEqual(len(schema_editor.deferred_sql), 0)

    def test_create_model(self):

        class TextDocument(models.Model):
            title = models.CharField(max_length=128)
            search = SearchVectorField([
                WeightedColumn('title', 'A'),
            ], 'english')

        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(TextDocument)
            self.assertEqual(len(schema_editor.deferred_sql), 1)
            self.assertIn('CREATE INDEX', schema_editor.deferred_sql[0])

        with TriggerSchemaEditor(connection) as schema_editor:
            schema_editor.create_model(TextDocument)
            self.assertEqual(len(schema_editor.deferred_sql), 2)
            self.assertIn('CREATE FUNCTION', schema_editor.deferred_sql[0])
            self.assertIn('CREATE TRIGGER', schema_editor.deferred_sql[1])


@isolate_apps('tests', attr_name='apps')
class MigrationTests(TestCase):

    create_model = migrations.CreateModel(
        'textdocument', [
            ('title', models.CharField(max_length=128)),
            ('body', models.TextField()),
            ('search', SearchVectorField([WeightedColumn('body', 'A')], 'english')),
        ]
    )

    delete_model = migrations.DeleteModel('textdocument')

    create_model_without_search = migrations.CreateModel(
        'textdocument', [
            ('body', models.TextField()),
        ]
    )

    add_field = migrations.AddField(
        'textdocument', 'search',
        SearchVectorField([WeightedColumn('body', 'A')], 'english')
    )

    alter_field = migrations.AlterField(
        'textdocument', 'search',
        SearchVectorField([WeightedColumn('title', 'A'), WeightedColumn('body', 'D')], 'english')
    )

    remove_field = migrations.RemoveField(
        'textdocument', 'search'
    )

    def migrate(self, ops, state=None):
        class Migration(migrations.Migration):
            operations = ops
        migration = Migration('name', 'tests')
        inject_trigger_operations([(migration, False)])
        with connection.schema_editor() as schema_editor:
            return migration.apply(state or ProjectState.from_apps(self.apps), schema_editor)

    def test_create_model(self):
        self.assertFITNotExists()
        self.migrate([
            self.create_model
        ])
        self.assertFITExists()

    def test_add_field(self):
        self.assertFITNotExists()
        state = self.migrate([
            self.create_model_without_search
        ])
        self.assertFITNotExists()
        self.migrate([
            self.add_field
        ], state)
        self.assertFITExists()

    def test_remove_field(self):
        self.assertFITNotExists()
        state = self.migrate([
            self.create_model
        ])
        self.assertFITExists()
        self.migrate([
            self.remove_field
        ], state)
        self.assertFITNotExists()

    def test_alter_field(self):
        state = self.migrate([
            self.create_model
        ])
        self.assertFITExists()
        self.assertNotIn('title', self.get_function_src('search'))
        self.migrate([
            self.alter_field
        ], state)
        self.assertFITExists()
        self.assertIn('title', self.get_function_src('search'))

    def test_delete_model(self):
        state = self.migrate([
            self.create_model
        ])
        self.assertFITExists()
        self.migrate([
            self.delete_model
        ], state)
        self.assertFITNotExists()

    SEARCH_COL = 'tests_{table}_{column}_.{{8}}'
    FIT = [SEARCH_COL + '_func', SEARCH_COL, SEARCH_COL + '_trig']

    def assertFITExists(self, column='search', table='textdocument'):
        with_column = [fit.format(column=column, table=table) for fit in self.FIT]
        self.assertFunctionExists(with_column[0])
        self.assertIndexExists(with_column[1])
        self.assertTriggerExists(with_column[2])

    def assertFITNotExists(self, column='search', table='textdocument'):
        with_column = [fit.format(column=column, table=table) for fit in self.FIT]
        self.assertFunctionNotExists(with_column[0])
        self.assertIndexNotExists(with_column[1])
        self.assertTriggerNotExists(with_column[2])

    _sql_check_function = "select proname from pg_proc where proname ~ %s"

    def assertFunctionExists(self, name):
        return self.assertXExists(self._sql_check_function, name)

    def assertFunctionNotExists(self, name):
        return self.assertXNotExists(self._sql_check_function, name)

    _sql_check_trigger = "select tgname from pg_trigger where tgname ~ %s"

    def assertTriggerExists(self, name):
        return self.assertXExists(self._sql_check_trigger, name)

    def assertTriggerNotExists(self, name):
        return self.assertXNotExists(self._sql_check_trigger, name)

    _sql_check_index = "select indexname from pg_indexes where indexname ~ %s"

    def assertIndexExists(self, name):
        return self.assertXExists(self._sql_check_index, name)

    def assertIndexNotExists(self, name):
        return self.assertXNotExists(self._sql_check_index, name)

    def assertXExists(self, sql, x):
        self.assertTrue(self._does_x_exist(sql, x), x)

    def assertXNotExists(self, sql, x):
        self.assertFalse(self._does_x_exist(sql, x), x)

    def _does_x_exist(self, sql, x):
        with connection.cursor() as cursor:
            cursor.execute(sql, [x])
            return len(cursor.fetchall()) > 0

    def get_function_src(self, column='search', table='textdocument'):
        func = self.FIT[0].format(column=column, table=table)
        with connection.cursor() as cursor:
            cursor.execute("select prosrc from pg_proc where proname ~ %s", [func])
            return cursor.fetchone()[0]