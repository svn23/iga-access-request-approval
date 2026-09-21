package in.cnxy.connector;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.context.annotation.*;
import org.springframework.security.config.annotation.web.builders.HttpSecurity; import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.preauth.AbstractPreAuthenticatedProcessingFilter;
import org.springframework.security.web.util.matcher.AntPathRequestMatcher;
import org.springframework.web.filter.OncePerRequestFilter; import jakarta.servlet.*; import jakarta.servlet.http.*; import java.io.IOException;

@Configuration
class SecurityConfig {
  @Bean SecurityFilterChain chain(HttpSecurity http, ConnectorProperties p) throws Exception {
    if (p.apiToken()==null || p.apiToken().length()<32) throw new IllegalStateException("BOT_CONNECTOR_API_TOKEN must be at least 32 characters");
    http.csrf(c->c.disable()).authorizeHttpRequests(a->a.requestMatchers("/actuator/health","/notify","/api/v1/midpoint/events").permitAll().anyRequest().authenticated())
      // Rate limit runs first (before auth) so a flood of unauthenticated/bad requests is shed cheaply.
      .addFilterBefore(new RateLimitFilter(p.rateLimitPerMinute()), org.springframework.security.web.context.SecurityContextHolderFilter.class)
      .addFilterBefore(new BearerFilter(p.apiToken()), AbstractPreAuthenticatedProcessingFilter.class).httpBasic(b->b.disable()).formLogin(f->f.disable()); return http.build(); }
  static class BearerFilter extends OncePerRequestFilter { final String expected; BearerFilter(String e){expected=e;}
    @Override protected boolean shouldNotFilter(HttpServletRequest r){ String path=r.getServletPath(); return "/actuator/health".equals(path)||"/notify".equals(path)||"/api/v1/midpoint/events".equals(path); }
    protected void doFilterInternal(HttpServletRequest r,HttpServletResponse s,FilterChain c)throws ServletException,IOException { String h=r.getHeader("Authorization"); if(h==null||!h.startsWith("Bearer ")||!java.security.MessageDigest.isEqual(expected.getBytes(java.nio.charset.StandardCharsets.UTF_8),h.substring(7).getBytes(java.nio.charset.StandardCharsets.UTF_8))){s.sendError(401);return;} var auth=new org.springframework.security.authentication.UsernamePasswordAuthenticationToken("teams-bot",null,java.util.List.of(()->"ROLE_BOT")); org.springframework.security.core.context.SecurityContextHolder.getContext().setAuthentication(auth); c.doFilter(r,s); } }
}
