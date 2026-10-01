---
layout: page
permalink: /teaching/
title: teaching
description: Course materials, schedules, and resources for classes taught.
nav: false # TODO(bendall): hidden; only template sample courses exist (and the template calendar). Decide whether to list courses
nav_order: 6
calendar: true
---

This page displays a collection of courses with detailed schedules, materials, and resources. You can organize your courses by years, terms, or topics.

{% include calendar.liquid calendar_id='test@gmail.com' timezone='Asia/Shanghai' %}

{% include courses.liquid %}
