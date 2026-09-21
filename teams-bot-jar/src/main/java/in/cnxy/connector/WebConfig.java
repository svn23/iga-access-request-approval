package in.cnxy.connector;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Web configuration to register request/response logging interceptor
 */
@Configuration
class WebConfig implements WebMvcConfigurer {
    private final RequestResponseLogger requestResponseLogger;

    WebConfig(RequestResponseLogger requestResponseLogger) {
        this.requestResponseLogger = requestResponseLogger;
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(requestResponseLogger)
                .addPathPatterns("/api/v1/**")
                .excludePathPatterns("/actuator/**");
    }
}
