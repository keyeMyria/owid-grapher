# -*- coding: utf-8 -*-
# Generated by Django 1.11 on 2018-02-16 00:50
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('grapher_admin', '0031_auto_20180216_0021'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='chartdimension',
            name='conversionFactor',
        ),
        migrations.RemoveField(
            model_name='chartdimension',
            name='displayName',
        ),
        migrations.RemoveField(
            model_name='chartdimension',
            name='isProjection',
        ),
        migrations.RemoveField(
            model_name='chartdimension',
            name='numDecimalPlaces',
        ),
        migrations.RemoveField(
            model_name='chartdimension',
            name='shortUnit',
        ),
        migrations.RemoveField(
            model_name='chartdimension',
            name='targetYear',
        ),
        migrations.RemoveField(
            model_name='chartdimension',
            name='tolerance',
        ),
        migrations.RemoveField(
            model_name='chartdimension',
            name='unit',
        ),
    ]
