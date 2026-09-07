<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { GraphsResponse } from "@generated/anki/stats_pb";
    import * as tr from "@generated/ftl";

    import AxisTicks from "./AxisTicks.svelte";
    import Graph from "./Graph.svelte";
    import type { GraphPrefs } from "./graph-helpers";
    import { defaultGraphBounds, GraphRange, RevlogRange } from "./graph-helpers";
    import GraphRangeRadios from "./GraphRangeRadios.svelte";
    import HoverColumns from "./HoverColumns.svelte";
    import InputBox from "./InputBox.svelte";
    import NoDataOverlay from "./NoDataOverlay.svelte";
    import type { VelocityGraphData } from "./velocity";
    import { gatherData, renderVelocityChart, VelocityMode } from "./velocity";

    export let sourceData: GraphsResponse | null = null;
    export let prefs: GraphPrefs | null = null;
    export let revlogRange: RevlogRange;
    // Accepted for parity with the other graphs; unused.
    export let nightMode: boolean = false;
    $: (void prefs, nightMode);

    const bounds = defaultGraphBounds();
    let svg: HTMLElement | SVGElement | null = null;
    let mode: VelocityMode = VelocityMode.stock;
    let graphRange: GraphRange = GraphRange.Year;

    let velocityData: VelocityGraphData | null = null;
    $: if (sourceData) {
        velocityData = gatherData(sourceData);
    }

    $: if (svg) {
        renderVelocityChart(svg as SVGElement, bounds, velocityData, mode, graphRange);
    }

    const title = tr.statisticsVelocityTitle();
    const subtitle = tr.statisticsVelocitySubtitle();
</script>

<Graph {title} {subtitle}>
    <InputBox>
        <label title={tr.statisticsVelocityStockTooltip()}>
            <input type="radio" bind:group={mode} value={VelocityMode.stock} />
            {tr.statisticsVelocityStock()}
        </label>
        <label title={tr.statisticsVelocityVelocityTooltip()}>
            <input type="radio" bind:group={mode} value={VelocityMode.velocity} />
            {tr.statisticsVelocityVelocity()}
        </label>
        <label title={tr.statisticsVelocityEfficiencyTooltip()}>
            <input type="radio" bind:group={mode} value={VelocityMode.efficiency} />
            {tr.statisticsVelocityEfficiency()}
        </label>

        <GraphRangeRadios bind:graphRange {revlogRange} followRevlog={true} />
    </InputBox>

    <svg bind:this={svg} viewBox={`0 0 ${bounds.width} ${bounds.height}`}>
        <HoverColumns />
        <AxisTicks {bounds} />
        <NoDataOverlay {bounds} />
    </svg>
</Graph>
