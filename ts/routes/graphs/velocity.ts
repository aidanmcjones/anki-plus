// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { GraphsResponse } from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { localizedDate } from "@tslib/i18n";
import { axisBottom, axisLeft, bisector, line, max, min, pointer, scaleLinear, scaleTime, select } from "d3";

import type { GraphBounds } from "./graph-helpers";
import { GraphRange, numericMap, setDataAvailable } from "./graph-helpers";
import { hideTooltip, showTooltip } from "./tooltip-utils.svelte";

export enum VelocityMode {
    stock,
    velocity,
    efficiency,
}

export interface VelocityPoint {
    /** Day offset; 0 = today, negative = past. */
    day: number;
    value: number;
}

export interface VelocityGraphData {
    /** Knowledge stock per sampled day, ascending by day. */
    stock: VelocityPoint[];
    /** Change in stock per day, per sampled stride. */
    velocity: VelocityPoint[];
    /** 7-day rolling knowledge gained per hour studied; null when no study time. */
    efficiency: VelocityPoint[];
    resolution: number;
}

/** Rolling window length for the efficiency series, in days. */
const EFFICIENCY_WINDOW = 7;

export function gatherData(data: GraphsResponse): VelocityGraphData | null {
    const velocity = data.velocity;
    if (!velocity || Object.keys(velocity.knowledge).length === 0) {
        return null;
    }
    const knowledge = numericMap(velocity.knowledge);
    const resolution = Math.max(1, velocity.resolutionDays);
    const days = [...knowledge.keys()].sort((a, b) => a - b);
    const minDay = days[0];

    // Stock: forward-fill across sampled gaps so the line is continuous.
    const stock: VelocityPoint[] = [];
    let previous = 0;
    for (let day = minDay; day <= 0; day++) {
        previous = knowledge.get(day) ?? previous;
        stock.push({ day, value: previous });
    }

    // Velocity: delta over one resolution stride, normalised per day.
    const velocitySeries: VelocityPoint[] = [];
    for (let i = resolution; i < stock.length; i++) {
        velocitySeries.push({
            day: stock[i].day,
            value: (stock[i].value - stock[i - resolution].value) / resolution,
        });
    }

    // Study time per day, in hours.
    const studyMillis = data.reviews ? numericMap(data.reviews.time) : new Map();
    const hoursOnDay = (day: number): number => {
        const entry = studyMillis.get(day);
        if (!entry) {
            return 0;
        }
        const millis = entry.learn + entry.relearn + entry.young + entry.mature
            + entry.filtered;
        return millis / 1000 / 60 / 60;
    };

    // Efficiency: rolling positive knowledge gain per rolling hour studied.
    const efficiency: VelocityPoint[] = [];
    for (let i = EFFICIENCY_WINDOW; i < stock.length; i++) {
        let gained = 0;
        let hours = 0;
        for (let j = i - EFFICIENCY_WINDOW + 1; j <= i; j++) {
            const delta = stock[j].value - stock[j - 1].value;
            if (delta > 0) {
                gained += delta;
            }
            hours += hoursOnDay(stock[j].day);
        }
        if (hours > 0) {
            efficiency.push({ day: stock[i].day, value: gained / hours });
        }
    }

    return { stock, velocity: velocitySeries, efficiency, resolution };
}

export function seriesForMode(
    data: VelocityGraphData,
    mode: VelocityMode,
): VelocityPoint[] {
    switch (mode) {
        case VelocityMode.stock:
            return data.stock;
        case VelocityMode.velocity:
            return data.velocity;
        case VelocityMode.efficiency:
            return data.efficiency;
    }
}

function rangeCutoff(range: GraphRange): number {
    switch (range) {
        case GraphRange.Month:
            return -31;
        case GraphRange.ThreeMonths:
            return -90;
        case GraphRange.Year:
            return -365;
        case GraphRange.AllTime:
            return -Infinity;
    }
}

export function renderVelocityChart(
    svgElem: SVGElement,
    bounds: GraphBounds,
    data: VelocityGraphData | null,
    mode: VelocityMode,
    range: GraphRange,
): void {
    const svg = select(svgElem);
    svg.selectAll(".lines").remove();
    svg.selectAll(".hover-columns").remove();
    svg.selectAll(".focus-line").remove();

    const cutoff = rangeCutoff(range);
    const points = data
        ? seriesForMode(data, mode).filter((p) => p.day >= cutoff)
        : [];
    if (points.length < 2) {
        setDataAvailable(svg, false);
        return;
    }

    const today = new Date();
    const dayToDate = (day: number): Date => new Date(today.getTime() + day * 86_400_000);
    const dated = points.map((p) => ({ x: dayToDate(p.day), y: p.value }));

    const x = scaleTime()
        .domain([dated[0].x, dated[dated.length - 1].x])
        .range([bounds.marginLeft, bounds.width - bounds.marginRight]);
    const yMax = max(dated, (d) => d.y)!;
    const yMin = Math.min(0, min(dated, (d) => d.y)!);
    const y = scaleLinear()
        .range([bounds.height - bounds.marginBottom, bounds.marginTop])
        .domain([yMin, yMax])
        .nice();

    const trans = svg.transition().duration(600) as any;
    svg.select<SVGGElement>(".x-ticks")
        .call((selection) => selection.transition(trans).call(axisBottom(x).ticks(7).tickSizeOuter(0)))
        .attr("direction", "ltr");
    svg.select<SVGGElement>(".y-ticks")
        .call((selection) =>
            selection.transition(trans).call(
                axisLeft(y)
                    .ticks(bounds.height / 50)
                    .tickSizeOuter(0),
            )
        )
        .attr("direction", "ltr");

    svg.append("g")
        .attr("class", "lines")
        .attr("fill", "none")
        .attr("stroke", "currentColor")
        .attr("stroke-width", 1.5)
        .attr("stroke-linejoin", "round")
        .attr("stroke-linecap", "round")
        .append("path")
        .attr("vector-effect", "non-scaling-stroke")
        .attr("d", line()(dated.map((d) => [x(d.x)!, y(d.y)!])));

    const focusLine = svg.append("line")
        .attr("class", "focus-line")
        .attr("y1", bounds.marginTop)
        .attr("y2", bounds.height - bounds.marginBottom)
        .attr("stroke", "currentColor")
        .attr("stroke-width", 1)
        .style("opacity", 0);

    const formatY = (value: number): string => {
        switch (mode) {
            case VelocityMode.stock:
                return `${tr.statisticsVelocityStock()}: ${value.toFixed(1)}`;
            case VelocityMode.velocity:
                return `${tr.statisticsVelocityVelocity()}: ${value.toFixed(2)}`;
            case VelocityMode.efficiency:
                return `${tr.statisticsVelocityEfficiency()}: ${value.toFixed(2)}`;
        }
    };

    const barWidth = (bounds.width - bounds.marginLeft - bounds.marginRight)
        / dated.length;
    const bisect = bisector((d: { x: Date }) => d.x).left;
    svg.append("g")
        .attr("class", "hover-columns")
        .selectAll("rect")
        .data(dated)
        .join("rect")
        .attr("x", (d) => x(d.x)! - barWidth / 2)
        .attr("y", bounds.marginTop)
        .attr("width", barWidth)
        .attr("height", bounds.height - bounds.marginTop - bounds.marginBottom)
        .attr("fill", "transparent")
        .on("mousemove", (event: MouseEvent) => {
            pointer(event, document.body);
            const date = x.invert(pointer(event, svgElem)[0]);
            const point = dated[Math.min(bisect(dated, date), dated.length - 1)];
            focusLine
                .attr("x1", x(point.x)!)
                .attr("x2", x(point.x)!)
                .style("opacity", 1);
            showTooltip(
                `${localizedDate(point.x)}<br>${formatY(point.y)}`,
                event.pageX,
                event.pageY,
            );
        })
        .on("mouseout", () => {
            focusLine.style("opacity", 0);
            hideTooltip();
        });

    setDataAvailable(svg, true);
}
