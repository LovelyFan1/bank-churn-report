import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart, RadarChart, ScatterChart, HeatmapChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, TitleComponent, RadarComponent, VisualMapComponent, MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
echarts.use([BarChart, LineChart, PieChart, RadarChart, ScatterChart, HeatmapChart,
  GridComponent, TooltipComponent, LegendComponent, TitleComponent, RadarComponent,
  VisualMapComponent, MarkLineComponent, CanvasRenderer])
export { echarts }
